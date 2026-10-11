// CustomGrandPrix (UE4SS C++ mod) -- step 1: read-only native hook of the Grand Prix list.
//
// The cup list the course-select menu shows is built natively by
// UGrandPrixUtilityLibrary::CalcSelectableGrandPrixIds() (the GPRecon Lua mod never saw a Blueprint call to it).
// Its UFunction's exec thunk is the only thing reflection gives us, so the real function is found by
// disassembling that thunk (Zydis) and following its call. A PolyHook detour on it logs every cup list it
// returns. Nothing is changed: the detour returns the original result untouched.
//
// Everything goes to UE4SS.log with the prefix [CustomGrandPrix]; the thunk's disassembly is logged too, so
// the address resolution can be checked by hand.

#include <Mod/CppUserModBase.hpp>
#include <DynamicOutput/DynamicOutput.hpp>
#include <Unreal/UObjectGlobals.hpp>
#include <Unreal/UObject.hpp>
#include <Unreal/CoreUObject/UObject/Class.hpp>
#include <Unreal/UFunctionStructs.hpp>
#include <Unreal/Core/HAL/UnrealMemory.hpp>
#include <Unreal/CoreUObject/UObject/UnrealType.hpp>
#include <Unreal/NameTypes.hpp>
#include <Unreal/Property/FSoftObjectProperty.hpp>
#include <Unreal/Property/FStructProperty.hpp>
#include <Unreal/Property/FStrProperty.hpp>
#include <Unreal/Hooks/Hooks.hpp>
#include <Unreal/FWeakObjectPtr.hpp>

#include <Zydis/Zydis.h>
#include <polyhook2/Detour/x64Detour.hpp>

#include <Windows.h>
#include <Psapi.h>

#include <algorithm>
#include <cmath>
#include <atomic>
#include <cstring>
#include <format>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

using namespace RC;
using namespace RC::Unreal;

namespace
{
    constexpr const CharType* kFunctionPath = STR("/Script/UNION.GrandPrixUtilityLibrary:CalcSelectableGrandPrixIds");
    constexpr size_t kMaxThunkBytes = 0x100;

    // TArray<EGrandPrixId> as laid out in memory (EGrandPrixId is uint8)
    struct GrandPrixIdArray
    {
        uint8_t* Data;
        int32_t Num;
        int32_t Max;
    };

    // static TArray<EGrandPrixId> CalcSelectableGrandPrixIds(): MSVC x64 returns the TArray through a hidden
    // pointer (RCX) and hands it back in RAX.
    using CalcSelectableFn = GrandPrixIdArray* (*)(GrandPrixIdArray*);
    uint64_t g_trampoline = 0;
    int g_calls = 0;

    // The custom cup is cup 13 = EGrandPrixId::Collabo_03, a placeholder cup the game hides. It is offered only
    // in the offline Grand Prix mode (cup_active); F10 turns it off/on. The menu reads the list when it opens.
    constexpr uint8_t kExtraCup = 13;
    std::atomic<bool> g_add_cup{true};
    // Cleared at start when the ids this mod borrows no longer mean what they meant for game 1.4.3
    // (check_game_version): then the mod changes nothing.
    std::atomic<bool> g_game_ok{true};
    auto clone_stage_rows() -> void;
    auto redirect_sound(bool custom, bool quiet = false) -> void;
    int g_redirect_pending = -1;  // a redirect still to do (1 custom, 0 stock): the sheet object did not exist yet
    double g_clock = 0.0;  // the mod's clock: seconds of engine ticks (tick_balloons)
    double g_redirect_until = 0.0;  // when the pending redirect is given up (mod clock, s)
    auto add_name_bubble(UObject* screen) -> void;
    auto lay_out_new_emblem(UObject* screen, UObject* icon, UObject* left, UObject* before_left) -> void;
    auto apply_badge(UObject* icon) -> void;
    auto ensure_music_sheet() -> void;
    auto init_balloons() -> void;
    auto sheet_state(const CharType* name) -> StringType;

    auto append_cup(GrandPrixIdArray* list, uint8_t cup) -> bool
    {
        for (int32_t i = 0; i < list->Num; ++i)
        {
            if (list->Data[i] == cup) return false;
        }
        if (list->Num == list->Max)
        {
            // grown through the engine's allocator, like TArray itself would
            int32_t new_max = list->Max + 4;
            auto data = static_cast<uint8_t*>(FMemory::Realloc(list->Data, static_cast<SIZE_T>(new_max)));
            if (!data) return false;
            list->Data = data;
            list->Max = new_max;
        }
        list->Data[list->Num++] = cup;
        return true;
    }

    auto widen(const char* s) -> StringType
    {
        StringType out;
        for (; *s; ++s) out += static_cast<CharType>(*s);
        return out;
    }

    // Raw TArray layout (Data, Num, Max), element type given by the caller.
    template <typename T>
    struct RawArray
    {
        T* Data;
        int32_t Num;
        int32_t Max;
    };

    // A reflected property of the object's class or any parent class (UObject::GetPropertyByName only looks at
    // the object's own class: the maps and bIsFocusable live on parents).
    auto find_property(UObject* object, const CharType* name) -> FProperty*
    {
        for (FProperty* p : object->GetClassPrivate()->ForEachPropertyInChain())
        {
            if (p->GetName() == name) return p;
        }
        return nullptr;
    }

    // Calls a UFunction through ProcessEvent with a parameter buffer laid out from its reflected properties.
    struct FnCall
    {
        UFunction* fn = nullptr;
        std::vector<uint8_t> buf;

        explicit FnCall(const CharType* path)
        {
            fn = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path);
            if (fn) buf.assign(static_cast<size_t>(std::max<int32_t>(fn->GetPropertiesSize(), 0)) + 16, 0);
            else Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] function {} not found\n"), path);
        }
        auto prop(const CharType* name) -> FProperty*
        {
            if (!fn) return nullptr;
            for (FProperty* p : fn->ForEachProperty())
            {
                if (p->GetName() == name) return p;
            }
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] {} has no parameter {}\n"), fn->GetName(), name);
            return nullptr;
        }
        auto set_raw(const CharType* name, const void* value, size_t size) -> bool
        {
            auto* p = prop(name);
            if (!p || size > static_cast<size_t>(p->GetSize())) return false;
            std::memcpy(buf.data() + p->GetOffset_Internal(), value, size);
            return true;
        }
        template <typename T>
        auto set(const CharType* name, T value) -> bool { return set_raw(name, &value, sizeof(T)); }
        // the parameter's bytes in the call buffer (nullptr if it has none)
        auto ptr(const CharType* name) -> uint8_t*
        {
            auto* p = prop(name);
            return p ? buf.data() + p->GetOffset_Internal() : nullptr;
        }
        template <typename T>
        auto get(const CharType* name) -> T
        {
            T value{};
            if (auto* p = prop(name)) std::memcpy(&value, buf.data() + p->GetOffset_Internal(), sizeof(T));
            return value;
        }
        auto call(UObject* target) -> bool
        {
            if (!fn || !target) return false;
            target->ProcessEvent(fn, buf.data());
            return true;
        }
    };

    // EGameModeId of the race being set up (AppRaceConfigDataAccessor::GetSelectedGameModeId): 1 = GrandPrix.
    // Every other mode (party race, time trial, rank / friend match, festival, legend...) never sees the cup.
    constexpr int kGrandPrixMode = 1;

    auto game_mode() -> int
    {
        auto* accessor = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/UNION.Default__AppRaceConfigDataAccessor"));
        if (!accessor) return -1;
        FnCall get(STR("/Script/UNION.AppRaceConfigDataAccessor:GetSelectedGameModeId"));
        if (!get.call(accessor)) return -1;
        return get.get<uint8_t>(STR("ReturnValue"));
    }

    auto cup_active() -> bool
    {
        return g_add_cup && g_game_ok && game_mode() == kGrandPrixMode;
    }

    // Appends to a TArray in place, grown through the engine allocator like TArray itself.
    template <typename T>
    auto append_raw(RawArray<T>* array, T value) -> bool
    {
        if (array->Num == array->Max)
        {
            int32_t new_max = array->Max + 4;
            auto data = static_cast<T*>(FMemory::Realloc(array->Data, sizeof(T) * static_cast<SIZE_T>(new_max)));
            if (!data) return false;
            array->Data = data;
            array->Max = new_max;
        }
        array->Data[array->Num++] = value;
        return true;
    }

    // Inserts into a TArray in place at `index` (clamped), grown through the engine allocator.
    template <typename T>
    auto insert_raw(RawArray<T>* array, int32_t index, T value) -> bool
    {
        if (!append_raw(array, value)) return false;
        index = (std::max)(0, (std::min)(index, array->Num - 1));
        for (int32_t i = array->Num - 1; i > index; --i) array->Data[i] = array->Data[i - 1];
        array->Data[index] = value;
        return true;
    }

    // Loads an asset by object path (KismetSystemLibrary::LoadAsset_Blocking) and keeps it in memory for good
    // (root set): tables whose rows the mod adds, the custom cup's badge, the ambience sheet it redirects.
    auto load_asset(const CharType* package, const CharType* asset) -> UObject*
    {
        auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/Engine.Default__KismetSystemLibrary"));
        FnCall load(STR("/Script/Engine.KismetSystemLibrary:LoadAsset_Blocking"));
        auto* soft = load.ptr(STR("Asset"));
        if (!library || !soft) return nullptr;
        // TSoftObjectPtr: weak pointer (8), FSoftObjectPath {PackageName, AssetName, SubPathString}
        *reinterpret_cast<FName*>(soft + 8) = FName(package, FNAME_Add);
        *reinterpret_cast<FName*>(soft + 16) = FName(asset, FNAME_Add);
        if (!load.call(library)) return nullptr;
        auto* object = load.get<UObject*>(STR("ReturnValue"));
        if (object) object->SetRootSet();
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] loaded {}: {}\n"), asset, object ? STR("ok") : STR("NOT FOUND"));
        return object;
    }

    // Copies a slot property (by name) of one panel slot to another through its setter UFunction.
    auto copy_slot_property(UObject* from, UObject* to, const CharType* property, const CharType* setter, const CharType* param) -> bool
    {
        auto* p = find_property(from, property);
        auto* value = from->GetValuePtrByPropertyNameInChain<uint8_t>(property);
        if (!p || !value) return false;
        FnCall set(setter);
        return set.set_raw(param, value, static_cast<size_t>(p->GetSize())) && set.call(to);
    }

    // Buttons move focus through explicit navigation links (UUnionUIButtonBaseCore::SetNavigationExplicitWidget).
    // EUINavigation: Left 0, Right 1, Up 2, Down 3, Next 4, Previous 5.
    auto get_navigation(UObject* button, uint8_t direction) -> UObject*
    {
        FnCall get(STR("/Script/UnionUI.UnionUIButtonBaseCore:GetNavigationExplicitWidget"));
        get.set<uint8_t>(STR("Direction"), direction);
        if (!get.call(button)) return nullptr;
        return get.get<bool>(STR("ReturnValue")) ? get.get<UObject*>(STR("FocusToWidget")) : nullptr;
    }

    // On a designed button (it has a UWidgetNavigation object from the designer): the game's own setter.
    auto set_navigation(UObject* button, uint8_t direction, UObject* target) -> void
    {
        FnCall set(STR("/Script/UnionUI.UnionUIButtonBaseCore:SetNavigationExplicitWidget"));
        set.set<uint8_t>(STR("Direction"), direction);
        set.set<UObject*>(STR("FocusToWidget"), target);
        set.call(button);
    }

    // On a widget created at runtime: it has no navigation object, and the game's setter writes into it
    // unchecked (crashed, 2026-10-10). The engine's UWidget::SetNavigationRuleExplicit creates it first.
    auto set_navigation_new(UObject* widget, uint8_t direction, UObject* target) -> void
    {
        FnCall set(STR("/Script/UMG.Widget:SetNavigationRuleExplicit"));
        set.set<uint8_t>(STR("Direction"), direction);
        set.set<UObject*>(STR("InWidget"), target);
        set.call(widget);
    }

    // The new emblem sits right of `left` (the last bottom-row emblem): it takes over left's right/up/down/next
    // links (the bottom row wraps: right of the last emblem is the first), and left now leads right to it; the
    // emblem left's right link wrapped to now leads left to the new one.
    auto link_navigation(UObject* left, UObject* icon) -> void
    {
        const CharType* names[] = {STR("Left"), STR("Right"), STR("Up"), STR("Down"), STR("Next"), STR("Previous")};
        UObject* links[6]{};
        for (uint8_t d = 0; d < 6; ++d)
        {
            links[d] = get_navigation(left, d);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {} navigation {}: {}\n"), left->GetName(), names[d],
                                            links[d] ? links[d]->GetName() : STR("-"));
        }
        for (uint8_t d : {uint8_t{1}, uint8_t{2}, uint8_t{3}, uint8_t{4}})
        {
            if (links[d]) set_navigation_new(icon, d, links[d]);
        }
        set_navigation_new(icon, 0, left);
        set_navigation_new(icon, 5, left);
        if (UObject* wrapped = links[1]; wrapped && get_navigation(wrapped, 0) == left)
        {
            set_navigation_new(wrapped, 0, icon);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {} navigation Left now: new emblem\n"), wrapped->GetName());
        }
        set_navigation_new(left, 1, icon);
        if (links[4]) set_navigation_new(left, 4, icon);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] navigation linked\n"));
    }

    // The designed emblems' button events (OnFocusEvent, OnDecisionEvent, ...) are bound by the course-select
    // screen when it is built -- before the new emblem exists -- so focusing it never told the screen. Each event
    // is a multicast delegate: TArray<FScriptDelegate {FWeakObjectPtr object, FName function}> (16 B each). An
    // event bound on the designed neighbour but empty on the new emblem gets the neighbour's bindings.
    auto copy_event_bindings(UObject* from, UObject* to) -> void
    {
        int copied = 0;
        for (FProperty* p : from->GetClassPrivate()->ForEachPropertyInChain())
        {
            if (!CastField<FMulticastInlineDelegateProperty>(p)) continue;
            auto* src = p->ContainerPtrToValuePtr<RawArray<uint8_t>>(from);
            auto* dst = p->ContainerPtrToValuePtr<RawArray<uint8_t>>(to);
            if (src->Num <= 0 || src->Num > 16 || dst->Num != 0) continue;
            constexpr size_t kEntry = 16;
            auto* data = static_cast<uint8_t*>(FMemory::Realloc(dst->Data, kEntry * static_cast<SIZE_T>(src->Num)));
            if (!data) continue;
            std::memcpy(data, src->Data, kEntry * static_cast<size_t>(src->Num));
            dst->Data = data;
            dst->Num = dst->Max = src->Num;
            ++copied;
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] event {}: {} binding(s) copied\n"), p->GetName(), src->Num);
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {} events bound\n"), copied);
    }

    // The new emblem's links (see link_navigation) were only good for the first visit: moving away from it lost
    // them (the screen apparently rewires the designed emblems on a cup change). After every emblem focus
    // change, the links into and out of the new emblem are set again.
    auto relink_new_emblems() -> void
    {
        std::vector<UObject*> widgets;
        UObjectGlobals::FindAllOf(STR("CourseSelectWidgetBase"), widgets);
        for (auto* w : widgets)
        {
            auto* emblems = w ? w->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixEmblemArr")) : nullptr;
            if (!emblems || emblems->Num != 11) continue;
            UObject* left = emblems->Data[9];
            UObject* icon = emblems->Data[10];
            UObject* wrapped = emblems->Data[7];  // first bottom-row emblem (SEGA GP 1): the row wraps around
            if (!left || !icon || !wrapped) continue;
            int fixed = 0;
            if (get_navigation(left, 1) != icon) { set_navigation_new(left, 1, icon); ++fixed; }
            if (get_navigation(wrapped, 0) != icon) { set_navigation_new(wrapped, 0, icon); ++fixed; }
            if (get_navigation(icon, 0) != left) { set_navigation_new(icon, 0, left); ++fixed; }
            if (get_navigation(icon, 1) != wrapped) { set_navigation_new(icon, 1, wrapped); ++fixed; }
            if (fixed) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] relinked {} navigation links\n"), fixed);
            lay_out_new_emblem(w, icon, left, emblems->Data[8]);
            apply_badge(icon);
        }
    }

    // Absolute (screen) centre X and local-to-absolute scale of a widget's last painted geometry.
    struct Placement { double centre = 0, scale = 0; };

    auto placement(UObject* widget) -> Placement
    {
        auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/UMG.Default__SlateBlueprintLibrary"));
        FnCall cached(STR("/Script/UMG.Widget:GetCachedGeometry"));
        if (!library || !cached.call(widget)) return {};
        auto* geometry = cached.ptr(STR("ReturnValue"));
        auto geometry_size = cached.prop(STR("ReturnValue"))->GetSize();
        FnCall local(STR("/Script/UMG.SlateBlueprintLibrary:GetLocalSize"));
        FnCall absolute(STR("/Script/UMG.SlateBlueprintLibrary:GetAbsoluteSize"));
        FnCall to_absolute(STR("/Script/UMG.SlateBlueprintLibrary:LocalToAbsolute"));
        local.set_raw(STR("Geometry"), geometry, static_cast<size_t>(geometry_size));
        absolute.set_raw(STR("Geometry"), geometry, static_cast<size_t>(geometry_size));
        to_absolute.set_raw(STR("Geometry"), geometry, static_cast<size_t>(geometry_size));
        if (!local.call(library) || !absolute.call(library)) return {};
        auto local_size = local.get<double>(STR("ReturnValue"));  // FVector2D.X (doubles)
        auto absolute_size = absolute.get<double>(STR("ReturnValue"));
        double half[2] = {local_size / 2, 0};
        to_absolute.set_raw(STR("LocalCoordinate"), half, sizeof(half));
        if (!to_absolute.call(library) || local_size <= 0) return {};
        return {to_absolute.get<double>(STR("ReturnValue")), absolute_size / local_size};
    }

    auto set_translation(UObject* widget, double x) -> void
    {
        FnCall set(STR("/Script/UMG.Widget:SetRenderTranslation"));
        double value[2] = {x, 0};
        set.set_raw(STR("Translation"), value, sizeof(value));
        set.call(widget);
    }

    std::vector<UObject*> g_laid_out;  // course-select screens already aligned
    UObject* g_badge = nullptr;         // UI_GPCustomCup13 (pipeline.cup_badge)

    auto parent_of(UObject* widget) -> UObject*
    {
        auto** slot_ptr = widget ? widget->GetValuePtrByPropertyNameInChain<UObject*>(STR("Slot")) : nullptr;
        auto** parent_ptr = slot_ptr && *slot_ptr ? (*slot_ptr)->GetValuePtrByPropertyNameInChain<UObject*>(STR("Parent")) : nullptr;
        return parent_ptr ? *parent_ptr : nullptr;
    }

    // Moves a widget sideways by `pixels` (screen) from its designed render translation; `scale` converts screen
    // pixels to the units of the panel it sits in.
    auto shift_by(UObject* widget, double pixels, double scale) -> void
    {
        double base = 0;
        if (auto* render = find_property(widget, STR("RenderTransform")))
        {
            base = *render->ContainerPtrToValuePtr<double>(widget);  // FWidgetTransform.Translation.X
        }
        set_translation(widget, base + pixels / scale);
    }

    // The bottom emblem row is centred by its layout: with a 4th emblem the three designed ones moved half a slot
    // left, away from their name bubbles (designed for 3), so the row stays centred and the bubbles follow: the
    // three designed bottom bubbles move half a slot left, the new emblem's bubble (a copy of the last one) half a
    // slot right of the last one's design position. Bubbles are converted with the scale of their own panel.
    // The top emblem nearest above the new one gets an Up/Down link to it. Done once per screen, after the
    // first paint (geometry is measured).
    auto lay_out_new_emblem(UObject* screen, UObject* icon, UObject* left, UObject* before_left) -> void
    {
        if (std::find(g_laid_out.begin(), g_laid_out.end(), screen) != g_laid_out.end()) return;
        auto a = placement(before_left), b = placement(left), c = placement(icon);
        if (a.scale <= 0 || b.scale <= 0 || c.scale <= 0) return;  // not painted yet
        g_laid_out.push_back(screen);
        double slot_abs = b.centre - a.centre;  // screen pixels between two emblems
        auto* balloons = screen->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixNameBalloons"));
        double bubble_scale = 0;
        if (balloons && balloons->Num == 11)
        {
            auto panel = placement(parent_of(balloons->Data[9]));
            bubble_scale = panel.scale > 0 ? panel.scale : b.scale;
            for (int32_t i = 7; i <= 9; ++i)
            {
                if (balloons->Data[i]) shift_by(balloons->Data[i], -slot_abs / 2, bubble_scale);
            }
            if (balloons->Data[10]) shift_by(balloons->Data[10], slot_abs / 2, bubble_scale);
        }
        // the top emblem above the new one: Down leads to it, and its Up leads back
        auto* emblems = screen->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixEmblemArr"));
        UObject* above = nullptr;
        double best = 1e9;
        for (int32_t i = 0; emblems && i < 7 && i < emblems->Num; ++i)
        {
            auto p = placement(emblems->Data[i]);
            if (p.scale > 0 && std::abs(p.centre - c.centre) < best)
            {
                best = std::abs(p.centre - c.centre);
                above = emblems->Data[i];
            }
        }
        if (above)
        {
            set_navigation(above, 3, icon);
            set_navigation_new(icon, 2, above);
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] layout: slot {:.1f} px, bubble panel scale {:.3f}, above: {}\n"),
                                        slot_abs, bubble_scale, above ? above->GetName() : StringType(STR("none")));
    }

    // The emblem's picture is a cell of the emblem atlas, chosen by the "Index" parameter of its images' dynamic
    // materials (the Blueprint maps cup 13 to cell 10, where pipeline.cup_badge puts the custom badge). The
    // created emblem never got it set (it showed cell 0), so it is set here, on every focus change.
    constexpr float kCustomCell = 10.0f;

    auto apply_badge(UObject* icon) -> void
    {
        if (!icon) return;
        for (const CharType* image_name : {STR("PAT_CourseIcon"), STR("PAT_CourseIcon_EmeraldMask")})
        {
            auto* image_property = find_property(icon, image_name);
            auto* image = image_property ? *image_property->ContainerPtrToValuePtr<UObject*>(icon) : nullptr;
            if (!image) continue;
            FnCall dynamic(STR("/Script/UMG.Image:GetDynamicMaterial"));
            if (!dynamic.call(image)) continue;
            auto* material = dynamic.get<UObject*>(STR("ReturnValue"));
            if (!material) continue;
            FnCall set(STR("/Script/Engine.MaterialInstanceDynamic:SetScalarParameterValue"));
            set.set<FName>(STR("ParameterName"), FName(STR("Index"), FNAME_Add));
            set.set<float>(STR("Value"), kCustomCell);
            set.call(material);
        }
    }

    // Adds key -> value to a TMap<uint8 enum, UObject*> property in place (engine layout, UE 5.4, 80 B):
    //   +0  sparse array Data (ptr, Num, Max) of elements {pair, HashNextId, HashIndex} (stride/offsets from the
    //       property layout), +16 TBitArray allocation flags (inline uint32[4], heap ptr, NumBits +40, MaxBits +44),
    //   +48 FirstFreeIndex, +52 NumFreeIndices, +56 hash buckets (inline FSetElementId, heap ptr +64), +72 HashSize.
    // Only the simple case: no free slots, room in the bit array, a power-of-two hash. Otherwise nothing changes.
    auto add_to_object_map(UObject* owner, const CharType* name, uint8_t key, UObject* value) -> bool
    {
        auto* property = CastField<FMapProperty>(find_property(owner, name));
        if (!property || property->GetSize() != 80) return false;
        auto* map = property->ContainerPtrToValuePtr<uint8_t>(owner);
        auto& layout = property->GetMapLayout();
        const int32_t stride = layout.SetLayout.Size;
        auto& data = *reinterpret_cast<uint8_t**>(map);
        auto& num = *reinterpret_cast<int32_t*>(map + 8);
        auto& max = *reinterpret_cast<int32_t*>(map + 12);
        auto* bits_heap = *reinterpret_cast<uint32_t**>(map + 32);
        auto* bits = bits_heap ? bits_heap : reinterpret_cast<uint32_t*>(map + 16);
        auto& num_bits = *reinterpret_cast<int32_t*>(map + 40);
        auto max_bits = *reinterpret_cast<int32_t*>(map + 44);
        auto num_free = *reinterpret_cast<int32_t*>(map + 52);
        auto* hash_heap = *reinterpret_cast<int32_t**>(map + 64);
        auto* buckets = hash_heap ? hash_heap : reinterpret_cast<int32_t*>(map + 56);
        auto hash_size = *reinterpret_cast<int32_t*>(map + 72);
        for (int32_t i = 0; i < num; ++i)
        {
            if (data[static_cast<size_t>(i) * stride] == key) return false;  // already there
        }
        if (num_free != 0 || num_bits != num || num_bits >= max_bits || hash_size <= 0 || (hash_size & (hash_size - 1)))
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] {}: unexpected map state (num {}, free {}, bits {}/{}, hash {}); not added\n"),
                                            name, num, num_free, num_bits, max_bits, hash_size);
            return false;
        }
        if (num == max)
        {
            auto* grown = static_cast<uint8_t*>(FMemory::Realloc(data, static_cast<SIZE_T>(stride) * static_cast<SIZE_T>(max + 4)));
            if (!grown) return false;
            data = grown;
            max += 4;
        }
        uint8_t* element = data + static_cast<size_t>(num) * stride;
        std::memset(element, 0, static_cast<size_t>(stride));
        element[0] = key;
        *reinterpret_cast<UObject**>(element + layout.ValueOffset) = value;
        int32_t bucket = key & (hash_size - 1);  // GetTypeHash of a uint8 enum is its value
        *reinterpret_cast<int32_t*>(element + layout.SetLayout.HashNextIdOffset) = buckets[bucket];
        *reinterpret_cast<int32_t*>(element + layout.SetLayout.HashIndexOffset) = bucket;
        buckets[bucket] = num;
        bits[num / 32] |= 1u << (num % 32);
        ++num_bits;
        ++num;
        return true;
    }

    // The cup's name bubble: a copy of the last designed bubble (same class, same canvas, same layout), appended to
    // GrandPrixNameBalloons. The screen's Blueprint then sets its text (the cup's GrandPrixName: "CUSTOM CUP",
    // pipeline.course_text) and shows it when the emblem is focused, like the others. Moved into place by
    // lay_out_new_emblem.
    auto add_name_bubble(UObject* screen) -> void
    {
        auto* balloons = screen->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixNameBalloons"));
        if (!balloons || balloons->Num != 10 || !balloons->Data[9]) return;
        UObject* last = balloons->Data[9];
        auto** slot_ptr = last->GetValuePtrByPropertyNameInChain<UObject*>(STR("Slot"));
        UObject* last_slot = slot_ptr ? *slot_ptr : nullptr;
        auto** parent_ptr = last_slot ? last_slot->GetValuePtrByPropertyNameInChain<UObject*>(STR("Parent")) : nullptr;
        UObject* parent = parent_ptr ? *parent_ptr : nullptr;
        auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/UMG.Default__WidgetBlueprintLibrary"));
        StringType parent_class = parent ? parent->GetClassPrivate()->GetName() : StringType(STR("none"));
        if (!parent || !library || (parent_class != STR("CanvasPanel") && parent_class != STR("Overlay")))
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] name bubble: parent {} is not a canvas or overlay; no bubble\n"), parent_class);
            return;
        }
        FnCall create(STR("/Script/UMG.WidgetBlueprintLibrary:Create"));
        create.set<UObject*>(STR("WorldContextObject"), screen);
        create.set<UObject*>(STR("WidgetType"), last->GetClassPrivate());
        create.set<UObject*>(STR("OwningPlayer"), nullptr);
        if (!create.call(library)) return;
        auto* bubble = create.get<UObject*>(STR("ReturnValue"));
        if (!bubble) return;
        bool canvas = parent_class == STR("CanvasPanel");
        FnCall add(canvas ? STR("/Script/UMG.CanvasPanel:AddChildToCanvas") : STR("/Script/UMG.Overlay:AddChildToOverlay"));
        add.set<UObject*>(STR("Content"), bubble);
        add.call(parent);
        auto* slot = add.get<UObject*>(STR("ReturnValue"));
        if (slot && canvas)
        {
            copy_slot_property(last_slot, slot, STR("LayoutData"), STR("/Script/UMG.CanvasPanelSlot:SetLayout"), STR("InLayoutData"));
            copy_slot_property(last_slot, slot, STR("bAutoSize"), STR("/Script/UMG.CanvasPanelSlot:SetAutoSize"), STR("InbAutoSize"));
            copy_slot_property(last_slot, slot, STR("ZOrder"), STR("/Script/UMG.CanvasPanelSlot:SetZOrder"), STR("InZOrder"));
        }
        else if (slot)
        {
            copy_slot_property(last_slot, slot, STR("Padding"), STR("/Script/UMG.OverlaySlot:SetPadding"), STR("InPadding"));
            copy_slot_property(last_slot, slot, STR("HorizontalAlignment"), STR("/Script/UMG.OverlaySlot:SetHorizontalAlignment"), STR("InHorizontalAlignment"));
            copy_slot_property(last_slot, slot, STR("VerticalAlignment"), STR("/Script/UMG.OverlaySlot:SetVerticalAlignment"), STR("InVerticalAlignment"));
        }
        if (auto* visibility = find_property(last, STR("Visibility")))
        {
            FnCall set_visibility(STR("/Script/UMG.Widget:SetVisibility"));
            set_visibility.set<uint8_t>(STR("InVisibility"), *visibility->ContainerPtrToValuePtr<uint8_t>(last));
            set_visibility.call(bubble);
        }
        if (auto* render = find_property(last, STR("RenderTransform")))  // the designed bubbles may be placed by it
        {
            FnCall set_render(STR("/Script/UMG.Widget:SetRenderTransform"));
            set_render.set_raw(STR("InTransform"), render->ContainerPtrToValuePtr<uint8_t>(last), static_cast<size_t>(render->GetSize()));
            set_render.call(bubble);
        }
        append_raw(balloons, bubble);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] name bubble {} added ({} bubbles)\n"), bubble->GetName(), balloons->Num);
    }

    // Step 3 (experiment, with F10): the course-select screen has 10 emblem widgets designed into it (top row:
    // the 7 colour cups, bottom row: SEGA GP 1 + 2 collaboration cups) and only shows cups that get one. A new
    // emblem of the same class goes into the bottom row (a HorizontalBox: it lines up by itself), with its
    // neighbour's slot settings, and is appended to the screen's emblem list so the game assigns it the extra cup.
    // Called from the first IsStoreRequiredForGrandPrix call of a screen: the cup list is built before the screen
    // exists, and the screen's Blueprint then walks its emblems calling that function for each one.
    auto add_emblem_slot(UObject* screen) -> void
    {
        for (auto* w : {screen})
        {
            if (!w) continue;
            auto* emblems = w->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixEmblemArr"));
            if (!emblems || emblems->Num != 10)
            {
                if (!emblems || emblems->Num < 10)
                {
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] emblem list has {} entries; no slot added\n"), emblems ? emblems->Num : -1);
                }
                continue;  // 11: already added for this screen
            }
            UObject* neighbour = emblems->Data[9];
            auto** slot_ptr = neighbour ? neighbour->GetValuePtrByPropertyNameInChain<UObject*>(STR("Slot")) : nullptr;
            UObject* neighbour_slot = slot_ptr ? *slot_ptr : nullptr;
            auto** parent_ptr = neighbour_slot ? neighbour_slot->GetValuePtrByPropertyNameInChain<UObject*>(STR("Parent")) : nullptr;
            UObject* row = parent_ptr ? *parent_ptr : nullptr;
            auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/UMG.Default__WidgetBlueprintLibrary"));
            if (!row || !library)
            {
                Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] bottom row or widget library not found; no slot added\n"));
                continue;
            }
            FnCall create(STR("/Script/UMG.WidgetBlueprintLibrary:Create"));
            create.set<UObject*>(STR("WorldContextObject"), w);
            create.set<UObject*>(STR("WidgetType"), neighbour->GetClassPrivate());
            create.set<UObject*>(STR("OwningPlayer"), nullptr);
            if (!create.call(library)) continue;
            auto* icon = create.get<UObject*>(STR("ReturnValue"));
            if (!icon)
            {
                Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] widget creation failed\n"));
                continue;
            }
            // Designed emblems are focusable (set in the designer); a created one has the class default, so focus
            // navigation could never land on it. bIsFocusable is only read when the Slate widget is built, so it is
            // copied before the emblem is added to the row.
            if (auto* focusable = CastField<FBoolProperty>(find_property(neighbour, STR("bIsFocusable"))))
            {
                bool value = focusable->GetPropertyValueInContainer(neighbour);
                focusable->SetPropertyValueInContainer(icon, value);
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] bIsFocusable copied: {}\n"), value);
            }
            FnCall add(STR("/Script/UMG.HorizontalBox:AddChildToHorizontalBox"));
            add.set<UObject*>(STR("Content"), icon);
            add.call(row);
            auto* slot = add.get<UObject*>(STR("ReturnValue"));
            if (slot)
            {
                copy_slot_property(neighbour_slot, slot, STR("Padding"), STR("/Script/UMG.HorizontalBoxSlot:SetPadding"), STR("InPadding"));
                copy_slot_property(neighbour_slot, slot, STR("Size"), STR("/Script/UMG.HorizontalBoxSlot:SetSize"), STR("InSize"));
                copy_slot_property(neighbour_slot, slot, STR("HorizontalAlignment"), STR("/Script/UMG.HorizontalBoxSlot:SetHorizontalAlignment"), STR("InHorizontalAlignment"));
                copy_slot_property(neighbour_slot, slot, STR("VerticalAlignment"), STR("/Script/UMG.HorizontalBoxSlot:SetVerticalAlignment"), STR("InVerticalAlignment"));
            }
            // append to the screen's emblem list (grown through the engine allocator)
            if (emblems->Num == emblems->Max)
            {
                int32_t new_max = emblems->Max + 4;
                auto data = static_cast<UObject**>(FMemory::Realloc(emblems->Data, sizeof(UObject*) * static_cast<SIZE_T>(new_max)));
                if (!data) continue;
                emblems->Data = data;
                emblems->Max = new_max;
            }
            // The screen's Blueprint reads each emblem's own GrandPrixId (set per emblem in the designer; a new one
            // defaults to 1 = Purple) while it walks the list.
            if (auto* id = icon->GetValuePtrByPropertyNameInChain<uint8_t>(STR("GrandPrixId"))) *id = kExtraCup;
            FnCall set_icon(STR("/Script/UNION.CourseSelectSubCategoryIcon:SetGrandprixIconId"));
            set_icon.set<uint8_t>(STR("IconId"), kExtraCup);
            set_icon.call(icon);
            link_navigation(neighbour, icon);
            copy_event_bindings(neighbour, icon);
            // designed emblems are Visible (0); a created one is SelfHitTestInvisible
            if (auto* visibility = find_property(neighbour, STR("Visibility")))
            {
                FnCall set_visibility(STR("/Script/UMG.Widget:SetVisibility"));
                set_visibility.set<uint8_t>(STR("InVisibility"), *visibility->ContainerPtrToValuePtr<uint8_t>(neighbour));
                set_visibility.call(icon);
            }
            if (auto* padding = find_property(neighbour, STR("Padding")))
            {
                FnCall set_padding(STR("/Script/UMG.UserWidget:SetPadding"));
                set_padding.set_raw(STR("InPadding"), padding->ContainerPtrToValuePtr<uint8_t>(neighbour), 16);
                set_padding.call(icon);
            }
            bool mapped = add_to_object_map(w, STR("GrandPrixIdAndEmblemMap"), kExtraCup, icon);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] GrandPrixIdAndEmblemMap += {}: {}\n"), kExtraCup, mapped ? STR("added") : STR("NOT added"));
            emblems->Data[emblems->Num++] = icon;
            // The screen's Blueprint copies the emblem list into MainButtons (+ the time-trial button) when it is
            // built, and on every focus change walks MainButtons to deselect all other emblems. Without the new
            // emblem in it, it was never deselected: it kept its selected look and could not be selected again.
            // It goes right after the designed emblems (before the time-trial button): a button's index in
            // MainButtons is also its name bubble's index in GrandPrixNameBalloons.
            if (auto* buttons = w->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("MainButtons")); buttons && buttons->Num >= 0 && buttons->Num < 32)
            {
                bool listed = false;
                for (int32_t i = 0; i < buttons->Num; ++i) listed = listed || buttons->Data[i] == icon;
                if (!listed && insert_raw(buttons, emblems->Num - 1, icon))
                {
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] MainButtons: new emblem at {} of {}\n"), emblems->Num - 1, buttons->Num);
                }
            }
            add_name_bubble(w);
            apply_badge(icon);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] added emblem {} to {} (slot {}); emblem list now {}\n"), icon->GetName(),
                                            row->GetName(), slot ? slot->GetClassPrivate()->GetName() : STR("none"), emblems->Num);
        }
    }

    // The GrandPrixId parameter of a hooked UFunction call (read like UE4SS's Lua hooks do: the function's
    // property offset into the frame's locals).
    auto cup_param(UnrealScriptFunctionCallableContext& context, UFunction* function) -> int
    {
        if (!context.TheStack.Locals()) return -1;
        for (FProperty* p : function->ForEachProperty())
        {
            if (p->GetName() == STR("GrandPrixId")) return *p->ContainerPtrToValuePtr<uint8_t>(context.TheStack.Locals());
        }
        return -1;
    }

    // Menu-only answers for the extra cup (the save is not touched): opened, and needing no store purchase.
    auto override_for_extra_cup(const CharType* path, bool answer) -> void
    {
        auto* function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path);
        if (!function)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] {} not found\n"), path);
            return;
        }
        function->RegisterPostHook([function, answer](UnrealScriptFunctionCallableContext& context, void*) {
            if (cup_param(context, function) == kExtraCup && context.RESULT_DECL && cup_active())
            {
                context.SetReturnValue<bool>(answer);
            }
        });
    }

    // Step 4 (experiment, with F10): new stage ids. The stage table already has rows for the unreleased debug
    // stages (3001...) pointing at data assets the registry knows but the game never shipped; the CustomGP pak
    // (pipeline.custom_stage) supplies DA_stg3001. To reach it without the cup screen, the first track the
    // game draws for the chosen cup (SelectGrandPrixAndCourseState::LotAndMakeGrandPrixMainStageIds, a
    // TArray<EStageId>) became stage 3001 (proof of concept, 2026-10-10: it loaded). Now the custom cup's own
    // row lists the new stages (pipeline.custom_stage), so this only logs each cup's tracks.

    auto replace_first_track() -> void
    {
        auto* function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, STR("/Script/UNION.SelectGrandPrixAndCourseState:LotAndMakeGrandPrixMainStageIds"));
        if (!function)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] LotAndMakeGrandPrixMainStageIds not found\n"));
            return;
        }
        function->RegisterPostHook([function](UnrealScriptFunctionCallableContext& context, void*) {
            auto* stages = static_cast<RawArray<uint8_t>*>(context.RESULT_DECL);
            if (!stages || stages->Num <= 0 || stages->Num > 16 || !stages->Data) return;
            StringType before;
            for (int32_t i = 0; i < stages->Num; ++i) before += (i ? STR(", ") : STR("")) + std::to_wstring(stages->Data[i]);
            int cup = cup_param(context, function);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] cup {} tracks [{}]\n"), cup, before);
            clone_stage_rows();
            // (the custom stage ships its own sound level, ambience cues and sheet SE_OBJ_ASR3001,
            // pipeline.ambience.own_sound_chain: E-Stadium's sheet is no longer renamed in memory)
        });
    }

    // The custom stages' menu pictures. Their rows in the stage table (CDT_StageDataAsset: the unreleased debug
    // stages 3001-3003) show a placeholder thumbnail and nothing else (black loading screen); the mod pak ships
    // the track's pictures as UI_Stage{Tmb,BG,Icon,Ring}3001 (pipeline.relocate_stage). The rows' soft object
    // paths are pointed at them in memory (the table file is left alone).
    // UDataTable: RowMap (TMap<FName, uint8*>) follows the RowStruct pointer; a TSoftObjectPtr is
    // {FWeakObjectPtr (8 B), FSoftObjectPath {PackageName FName, AssetName FName, SubPathString FString}} = 40 B.
    constexpr uint8_t kCustomStages[] = {74, 75, 76};  // EStageId::STG3001-3003
    static_assert(sizeof(FName) == 8, "FName is written into game memory: must match the engine layout");
    bool g_rows_patched = false;

    auto patch_stage_rows() -> void
    {
        if (g_rows_patched) return;
        auto* table = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Game/01_Union/Database/Stage/DataTable/CDT_StageDataAsset.CDT_StageDataAsset"));
        auto* row_struct_property = table ? find_property(table, STR("RowStruct")) : nullptr;
        if (!row_struct_property)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] stage table not found\n"));
            return;
        }
        auto* row_struct = *row_struct_property->ContainerPtrToValuePtr<UStruct*>(table);
        auto* row_map = reinterpret_cast<uint8_t*>(table) + row_struct_property->GetOffset_Internal() + 8;
        auto* rows = *reinterpret_cast<uint8_t**>(row_map);
        auto num = *reinterpret_cast<int32_t*>(row_map + 8);
        FProperty* stage_id = nullptr;
        struct Image { const CharType* property; const CharType* folder; const CharType* prefix; FProperty* found; };
        Image images[] = {{STR("ThumbnailImage"), STR("Texture_StageTmb"), STR("UI_StageTmb"), nullptr},
                          {STR("LoadBGImage"), STR("Texture_StageBG"), STR("UI_StageBG"), nullptr},
                          {STR("LoadIconImage"), STR("Texture_StageIcon"), STR("UI_StageIcon"), nullptr},
                          {STR("LobbyTravelRingImage"), STR("Texture_StageRing"), STR("UI_StageRing"), nullptr}};
        for (FProperty* p : row_struct->ForEachPropertyInChain())
        {
            if (p->GetName() == STR("StageId")) stage_id = p;
            for (auto& image : images)
            {
                if (p->GetName() == image.property && p->GetSize() == 40) image.found = p;
            }
        }
        if (!stage_id || !rows || num <= 0 || num > 512)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] stage table layout unexpected; pictures not set\n"));
            return;
        }
        int patched = 0;
        for (int32_t i = 0; i < num; ++i)
        {
            auto* row = *reinterpret_cast<uint8_t**>(rows + static_cast<size_t>(i) * 24 + 8);
            if (!row) continue;
            uint8_t id = *stage_id->ContainerPtrToValuePtr<uint8_t>(row);
            if (std::find(std::begin(kCustomStages), std::end(kCustomStages), id) == std::end(kCustomStages)) continue;
            for (auto& image : images)
            {
                if (!image.found) continue;
                auto* soft = image.found->ContainerPtrToValuePtr<uint8_t>(row);
                StringType asset = StringType(image.prefix) + STR("3001");
                StringType package = StringType(STR("/Game/01_Union/UI/System/")) + image.folder + STR("/") + asset;
                std::memset(soft, 0, 8);  // weak pointer: resolve again
                *reinterpret_cast<FName*>(soft + 8) = FName(package.c_str(), FNAME_Add);
                *reinterpret_cast<FName*>(soft + 16) = FName(asset.c_str(), FNAME_Add);
            }
            ++patched;
        }
        g_rows_patched = patched > 0;
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] stage table: pictures set on {} custom stage rows\n"), patched);
    }

    // ---- Per-stage table rows for the custom stages ----
    // Stage-keyed tables (row names "EStageId::STG1016" or "stg1016") have no rows for the new ids: no intro
    // cutscene, no podium / result scenes, no course-select card data, default car shadow... Each listed table
    // gets the template stage's row copied under every new stage's name (row names follow the template's
    // pattern), with its StageId set and soft paths to packages the mod ships under the new name redirected.
    // Rows the game already has for the debug stages (car shadow, time trial) are overwritten the same way.
    // In memory only: the table files stay stock; stock rows are never changed.
    constexpr uint8_t kTemplateStage = 15;  // EStageId::STG1016 (E-Stadium)
    constexpr const CharType* kTemplateNumber = STR("1016");
    constexpr const CharType* kStageNumbers[] = {STR("3001"), STR("3002"), STR("3003")};
    struct TableRule
    {
        const CharType* table;   // part of the table's name
        const CharType* source;  // row to copy, nullptr: the template stage's
    };
    const TableRule kRowTables[] = {
        {STR("StageCameraSettings"), STR("EStageId::STG1022")},  // cutscene exposure/bloom: Metal Harbor's day values
        {STR("IntroduceCourseSceneData"), nullptr},
        {STR("CeremonySceneData"), nullptr},
        {STR("FestaResultSceneData"), nullptr},
        {STR("StageDevelopProgress"), nullptr},  // course-select cards
        {STR("TargetTimePerStage"), nullptr},
        {STR("LineGuideData"), nullptr},
        {STR("TravelRingMainCourseData"), nullptr},
    };
    // The racer / rival introductions are lit darker on the custom stages than the reference stage's own values
    // expect (the race camera adapts, these use fixed exposure): their rows get this much more exposure (EV).
    constexpr float kIntroExposureBoost = 0.5f;  // scenery brightness; the racers are lit by turn_level_lights
    constexpr const CharType* kIntroTables[] = {STR("EntryRacers"), STR("PerformRival")};

    // packages the mod ships under the new stage's name (pipeline.relocate_stage)
    const std::pair<const CharType*, const CharType*> kRedirects[] = {
        {STR("/Game/01_Union/Scene/IntroduceCourse/LS_stg1016"), STR("/Game/01_Union/Scene/IntroduceCourse/LS_asr3001")},
        {STR("LS_stg1016"), STR("LS_asr3001")},
    };

    // GetTypeHash(FName) of this engine build (UE 5.4, numbers kept outside the name entry):
    // GetTypeHash(FNameEntryHandle{Block = id >> 16, Offset = id & 0xFFFF}) + Number.
    auto name_hash(const FName& name) -> uint32_t
    {
        uint32_t id = name.GetComparisonIndex().ToUnstableInt();
        uint32_t block = id >> 16, offset = id & 0xFFFF;
        return (block << (32 - 13)) + block + (offset << 16) + offset + (offset >> 4) + static_cast<uint32_t>(name.GetNumber());
    }

    // UDataTable::RowMap (TMap<FName, uint8*>), raw TMap layout as in add_to_object_map; elements are
    // {FName key, uint8* row, HashNextId, HashIndex} (24 B).
    struct RowMap
    {
        uint8_t* map;
        auto data() -> uint8_t*& { return *reinterpret_cast<uint8_t**>(map); }
        auto num() -> int32_t& { return *reinterpret_cast<int32_t*>(map + 8); }
        auto capacity() -> int32_t& { return *reinterpret_cast<int32_t*>(map + 12); }
        auto key(int32_t i) -> FName& { return *reinterpret_cast<FName*>(data() + static_cast<size_t>(i) * 24); }
        auto row(int32_t i) -> uint8_t*& { return *reinterpret_cast<uint8_t**>(data() + static_cast<size_t>(i) * 24 + 8); }
        auto hash_size() -> int32_t { return *reinterpret_cast<int32_t*>(map + 72); }
        auto buckets() -> int32_t*
        {
            auto* heap = *reinterpret_cast<int32_t**>(map + 64);
            return heap ? heap : reinterpret_cast<int32_t*>(map + 56);
        }
        auto find(const FName& name) -> int32_t
        {
            for (int32_t i = 0; i < num(); ++i)
            {
                if (key(i) == name) return i;
            }
            return -1;
        }
        // the table's own hashes agree with name_hash: inserting is safe
        auto hashes_ok() -> bool
        {
            int32_t size = hash_size();
            if (size <= 0 || (size & (size - 1))) return false;
            for (int32_t i = 0; i < num(); ++i)
            {
                auto stored = *reinterpret_cast<int32_t*>(data() + static_cast<size_t>(i) * 24 + 20);
                if (stored != static_cast<int32_t>(name_hash(key(i)) & static_cast<uint32_t>(size - 1))) return false;
            }
            return true;
        }
        auto insert(const FName& name, uint8_t* value) -> bool
        {
            auto num_free = *reinterpret_cast<int32_t*>(map + 52);
            auto& num_bits = *reinterpret_cast<int32_t*>(map + 40);
            auto& max_bits = *reinterpret_cast<int32_t*>(map + 44);
            auto*& bits_heap = *reinterpret_cast<uint32_t**>(map + 32);
            if (num_free != 0 || num_bits != num()) return false;
            if (num_bits >= max_bits)
            {
                // TBitArray<TInlineAllocator<4>>: past 128 bits the words live on the heap
                int32_t words = max_bits / 32 + 4;
                auto* grown = static_cast<uint32_t*>(FMemory::Malloc(sizeof(uint32_t) * static_cast<SIZE_T>(words)));
                if (!grown) return false;
                std::memset(grown, 0, sizeof(uint32_t) * static_cast<size_t>(words));
                std::memcpy(grown, bits_heap ? bits_heap : reinterpret_cast<uint32_t*>(map + 16), sizeof(uint32_t) * static_cast<size_t>(max_bits / 32));
                if (bits_heap) FMemory::Free(bits_heap);
                bits_heap = grown;
                max_bits = words * 32;
            }
            if (num() == capacity())
            {
                auto* grown = static_cast<uint8_t*>(FMemory::Realloc(data(), 24 * static_cast<SIZE_T>(capacity() + 8)));
                if (!grown) return false;
                data() = grown;
                capacity() += 8;
            }
            int32_t i = num();
            uint8_t* element = data() + static_cast<size_t>(i) * 24;
            std::memset(element, 0, 24);
            key(i) = name;
            row(i) = value;
            int32_t bucket = static_cast<int32_t>(name_hash(name) & static_cast<uint32_t>(hash_size() - 1));
            *reinterpret_cast<int32_t*>(element + 16) = buckets()[bucket];
            *reinterpret_cast<int32_t*>(element + 20) = bucket;
            buckets()[bucket] = i;
            auto* bits = bits_heap ? bits_heap : reinterpret_cast<uint32_t*>(map + 16);
            bits[i / 32] |= 1u << (i % 32);
            ++num_bits;
            ++num();
            return true;
        }
    };

    auto replaced(StringType text, const StringType& from, const StringType& to) -> StringType
    {
        if (auto at = text.find(from); at != StringType::npos) text.replace(at, from.size(), to);
        return text;
    }

    // Soft object paths in a row (and its nested structs) that point at a redirected package.
    auto redirect_soft_paths(UStruct* type, uint8_t* data) -> int
    {
        int changed = 0;
        for (FProperty* p : type->ForEachPropertyInChain())
        {
            if (auto* nested = CastField<FStructProperty>(p))
            {
                changed += redirect_soft_paths(nested->GetStruct(), p->ContainerPtrToValuePtr<uint8_t>(data));
                continue;
            }
            if (!CastField<FSoftObjectProperty>(p) || p->GetSize() != 40) continue;
            auto* soft = p->ContainerPtrToValuePtr<uint8_t>(data);
            for (int part : {8, 16})  // PackageName, AssetName
            {
                auto* name = reinterpret_cast<FName*>(soft + part);
                auto text = name->ToString();
                for (auto& [from, to] : kRedirects)
                {
                    if (text == from)
                    {
                        *name = FName(to, FNAME_Add);
                        std::memset(soft, 0, 8);  // weak pointer: resolve again
                        ++changed;
                    }
                }
            }
        }
        return changed;
    }

    std::vector<UObject*> g_cloned_tables;

    auto clone_stage_rows() -> void
    {
        if (!g_game_ok) return;
        std::vector<UObject*> tables;
        UObjectGlobals::FindAllOf(STR("DataTable"), tables);
        std::vector<UObject*> composites;
        UObjectGlobals::FindAllOf(STR("CompositeDataTable"), composites);
        tables.insert(tables.end(), composites.begin(), composites.end());
        for (auto* table : tables)
        {
            if (!table || std::find(g_cloned_tables.begin(), g_cloned_tables.end(), table) != g_cloned_tables.end()) continue;
            auto table_name = table->GetName();
            const TableRule* rule = nullptr;
            for (auto& r : kRowTables)
            {
                if (table_name.find(r.table) != StringType::npos) rule = &r;
            }
            if (!rule) continue;
            auto* row_struct_property = find_property(table, STR("RowStruct"));
            if (!row_struct_property) continue;
            auto* row_struct = *row_struct_property->ContainerPtrToValuePtr<UScriptStruct*>(table);
            RowMap rows{reinterpret_cast<uint8_t*>(table) + row_struct_property->GetOffset_Internal() + 8};
            if (!row_struct || rows.num() <= 0 || rows.num() > 4096 || !rows.data()) continue;
            g_cloned_tables.push_back(table);
            bool can_insert = rows.hashes_ok();
            FProperty* stage_id = nullptr;
            for (FProperty* p : row_struct->ForEachPropertyInChain())
            {
                if (p->GetName() == STR("StageId") && p->GetSize() == 1) stage_id = p;
            }
            int added = 0, overwritten = 0, redirected = 0;
            for (StringType prefix : {StringType(STR("EStageId::STG")), StringType(STR("stg"))})
            {
                int32_t template_row = rows.find(FName((prefix + kTemplateNumber).c_str(), FNAME_Add));
                if (template_row < 0) continue;
                int32_t source_row = rule->source ? rows.find(FName(rule->source, FNAME_Add)) : template_row;
                if (source_row < 0) continue;
                for (size_t k = 0; k < std::size(kStageNumbers); ++k)
                {
                    FName new_name((prefix + kStageNumbers[k]).c_str(), FNAME_Add);
                    uint8_t* source = rows.row(source_row);
                    uint8_t* target = nullptr;
                    if (int32_t existing = rows.find(new_name); existing >= 0)
                    {
                        target = rows.row(existing);
                        row_struct->CopyScriptStruct(target, source);
                        ++overwritten;
                    }
                    else
                    {
                        if (!can_insert) continue;
                        target = static_cast<uint8_t*>(FMemory::Malloc(static_cast<SIZE_T>(row_struct->GetStructureSize()),
                                                                       static_cast<uint32>((std::max<int16>)(row_struct->GetMinAlignment(), int16{8}))));
                        if (!target) continue;
                        row_struct->InitializeStruct(target);
                        row_struct->CopyScriptStruct(target, source);
                        if (!rows.insert(new_name, target))
                        {
                            row_struct->DestroyStruct(target);
                            FMemory::Free(target);
                            continue;
                        }
                        ++added;
                    }
                    if (stage_id) *stage_id->ContainerPtrToValuePtr<uint8_t>(target) = kCustomStages[k];
                    for (auto* intro : kIntroTables)
                    {
                        if (table_name.find(intro) == StringType::npos) continue;
                        for (FProperty* p : row_struct->ForEachPropertyInChain())
                        {
                            if (p->GetName() == STR("AutoExposureBias") && p->GetSize() == 4) *p->ContainerPtrToValuePtr<float>(target) += kIntroExposureBoost;
                        }
                    }
                    redirected += redirect_soft_paths(row_struct, target);
                }
            }
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {}: {} rows added, {} overwritten, {} paths redirected{}\n"), table_name, added,
                                            overwritten, redirected, can_insert ? STR("") : STR(" (hashes differ: no rows added)"));
        }
    }

    // The course-select cards ask whether each stage may be used in this mode / is installed: the debug stage
    // ids are not. For the custom stages, in the Grand Prix: yes.
    auto allow_custom_stages(const CharType* path) -> void
    {
        auto* function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path);
        if (!function)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] {} not found\n"), path);
            return;
        }
        function->RegisterPostHook([function](UnrealScriptFunctionCallableContext& context, void*) {
            if (!context.TheStack.Locals() || !context.RESULT_DECL) return;
            for (FProperty* p : function->ForEachProperty())
            {
                if (p->GetName() != STR("StageId")) continue;
                uint8_t id = *p->ContainerPtrToValuePtr<uint8_t>(context.TheStack.Locals());
                if (std::find(std::begin(kCustomStages), std::end(kCustomStages), id) != std::end(kCustomStages) && cup_active())
                {
                    context.SetReturnValue<bool>(true);
                }
            }
        });
    }

    // ---- Sound: the custom stages play the track's own music and ambience ----
    // Stage music comes from BGM.acb through E-Stadium's cue sheet BGM_STG1016 (the data asset's BGM label is
    // E-Stadium's), and the stage ambience from SE_OBJ_STG1016 (the data asset's CourseCueSheet, the sound level,
    // the intro's ambience cues). While a custom cup is raced these two sheet assets load the mod's copies
    // (CriWareData/BGM_ASR3001.acb/.awb, SE_OBJ_ASR3001.acb: the track's songs and ambience, same cue names);
    // for any other cup, E-Stadium's own files. The names have the same length: changed in place.
    const std::pair<const CharType*, const CharType*> kSheetRedirects[] = {{STR("STG1016"), STR("ASR3001")}};

    // Ambience: the stage's sheet (SE_OBJ_STG1016) is loaded with the race. While it is not loaded, its CueSheetName
    // (which names the CriWareData file, AcbFilePath being empty) is switched in place to the mod's copy for a custom
    // cup and back for any other cup; a loaded sheet is left alone (releasing a sheet the game holds crashed the
    // game, 2026-10-11).
    // Music: the custom stages have their own music label (pipeline.bgm_label: their data asset's BGMName,
    // BGM.acb branches for it and the label in the sound settings), whose branches play the track's own sheet
    // BGM_ASR3001 (its own name inside too). The mod only has to keep that sheet loaded, like the game keeps the
    // stock stage sheets: loaded once at startup (load_music_sheet). E-Stadium's sheet is never touched.
    std::vector<UObject*> g_sound_sheets;
    UObject* g_music_sheet = nullptr;

    auto set_in_place(FString* text, const StringType& from, const StringType& to) -> bool
    {
        if (!text || text->Len() <= 0) return false;
        StringType value(**text);
        auto at = value.find(from);
        if (at == StringType::npos) return false;
        for (size_t c = 0; c < to.size(); ++c) text->GetCharArray().GetData()[at + c] = to[c];
        return true;
    }

    auto sheet_loaded(UObject* sheet) -> bool
    {
        FnCall loaded(STR("/Script/CriWareRuntime.SoundAtomCueSheet:IsLoaded"));
        return loaded.call(sheet) && loaded.get<bool>(STR("ReturnValue"));
    }

    // The ambience sheet object can be gone when the cup is chosen (unloaded since the preload, 2026-10-11: the
    // race then loaded it under its stock name, with E-Stadium's ambience and no pop sound). The redirect then
    // stays pending and is retried every 0.1 s (tick_balloons) until the object is there again, before its
    // audio is loaded.
    auto redirect_sound(bool custom, bool quiet) -> void
    {
        // ambience
        std::vector<UObject*> sheets;
        UObjectGlobals::FindAllOf(STR("SoundAtomCueSheet"), sheets);
        if (!quiet) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] redirect_sound({}): {}\n"), custom, sheet_state(STR("SE_OBJ_STG1016")));
        bool found = false;
        for (auto* sheet : sheets)
        {
            if (!sheet || sheet->GetName() != STR("SE_OBJ_STG1016")) continue;
            found = true;
            auto* sheet_name = sheet->GetValuePtrByPropertyNameInChain<FString>(STR("CueSheetName"));
            if (!sheet_name) continue;
            StringType from = custom ? STR("STG1016") : STR("ASR3001"), to = custom ? STR("ASR3001") : STR("STG1016");
            StringType current(**sheet_name);
            if (current.find(from) == StringType::npos)
            {
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] ambience sheet already '{}'\n"), current);
                continue;
            }
            if (sheet_loaded(sheet))
            {
                Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] ambience sheet loaded as '{}': left as is\n"), current);
                continue;
            }
            set_in_place(sheet_name, from, to);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] ambience sheet: {} -> {}\n"), current, StringType(**sheet_name));
        }
        if (!found && !quiet) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] ambience sheet not loaded yet: redirect pending\n"));
        if (found) g_redirect_pending = -1;
        else if (!quiet)
        {
            g_redirect_pending = custom ? 1 : 0;
            g_redirect_until = g_clock + 60.0;  // a minute at most (e.g. the cup was left for a stock track)
        }
    }

    // Music diagnostics: the label each race-music call gets, and the state of the sheets involved.
    auto sheet_state(const CharType* name) -> StringType
    {
        std::vector<UObject*> sheets;
        UObjectGlobals::FindAllOf(STR("SoundAtomCueSheet"), sheets);
        for (auto* sheet : sheets)
        {
            if (!sheet || sheet->GetName() != name) continue;
            FnCall cues(STR("/Script/CriWareRuntime.SoundAtomCueSheet:GetNumCues"));
            int cue_count = cues.call(sheet) ? cues.get<int32_t>(STR("ReturnValue")) : -1;
            auto* sheet_name = sheet->GetValuePtrByPropertyNameInChain<FString>(STR("CueSheetName"));
            return std::format(STR("{} (CueSheetName '{}', loaded {}, cues {})"), name, sheet_name ? StringType(**sheet_name) : STR("?"),
                               sheet_loaded(sheet), cue_count);
        }
        return std::format(STR("{} (no object)"), name);
    }

    // Our level's sun (and its sky fills, pipeline.ue_build) shines from behind the racers on the starting grid:
    // the race camera sees their lit backs, the intro cameras their dark faces. The scenery is unlit, so these
    // lights only light racers and items: for the intro (PlayIntro) they are turned by 180 degrees, and back
    // when the race starts (PlayFirstLap).
    bool g_lights_turned = false;
    ULONGLONG g_intro_at = 0;  // ms (GetTickCount64) when the lights were turned for the intro

    auto turn_level_lights(bool intro) -> void
    {
        if (intro == g_lights_turned) return;
        std::vector<UObject*> lights;
        UObjectGlobals::FindAllOf(STR("DirectionalLight"), lights);
        int turned = 0;
        for (auto* light : lights)
        {
            auto* level = light ? light->GetOuterPrivate() : nullptr;
            auto* world = level ? level->GetOuterPrivate() : nullptr;
            if (!world || world->GetName().rfind(STR("SL_asr3001"), 0) != 0) continue;
            FnCall get(STR("/Script/Engine.Actor:K2_GetActorRotation"));
            if (!get.call(light)) continue;
            double rotation[3];  // FRotator {Pitch, Yaw, Roll}
            std::memcpy(rotation, get.ptr(STR("ReturnValue")), sizeof(rotation));
            rotation[1] += intro ? 180.0 : -180.0;
            FnCall set(STR("/Script/Engine.Actor:K2_SetActorRotation"));
            set.set_raw(STR("NewRotation"), rotation, sizeof(rotation));
            set.set<bool>(STR("bTeleportPhysics"), false);
            set.call(light);
            ++turned;
        }
        g_lights_turned = intro;
        if (intro) g_intro_at = GetTickCount64();
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {} level lights turned for the {}\n"), turned, intro ? STR("intro") : STR("race"));
    }

    auto log_music_calls() -> void
    {
        for (const CharType* fn : {STR("PlayIntro"), STR("PlayFirstLap"), STR("ChangeSecondLap"), STR("ChangeSecondLapForce"),
                                   STR("ChangeFinalLap"), STR("ChangeFinalLapForce"), STR("ChangeGateDecide"), STR("Stop")})
        {
            auto path = StringType(STR("/Script/UnionSystem.UnionRaceBGMLibrary:")) + fn;
            auto* function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path.c_str());
            if (!function) continue;
            function->RegisterPreHook([function](UnrealScriptFunctionCallableContext& context, void*) {
                StringType labels;
                if (context.TheStack.Locals())
                {
                    for (FProperty* p : function->ForEachProperty())
                    {
                        if (!CastField<FStrProperty>(p)) continue;
                        auto* text = p->ContainerPtrToValuePtr<FString>(context.TheStack.Locals());
                        labels += std::format(STR(" {}='{}'"), p->GetName(), StringType(**text));
                    }
                }
                if (labels.find(STR("stg3001_")) != StringType::npos)  // a custom stage's label
                {
                    ensure_music_sheet();  // (fallback)
                    if (function->GetName() == STR("PlayIntro")) turn_level_lights(true);
                    if (function->GetName() == STR("PlayIntro")) g_redirect_pending = -1;  // too late from here on
                    if (function->GetName() == STR("PlayFirstLap")) turn_level_lights(false);
                    if (function->GetName() == STR("PlayIntro") || function->GetName() == STR("PlayFirstLap")) init_balloons();
                }
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] music: {}{} | {} | {} | {}\n"), function->GetName(), labels,
                                                sheet_state(STR("BGM")), sheet_state(STR("BGM_STG1016")), sheet_state(STR("BGM_ASR3001")));
            });
        }
    }

    // Loaded at start, the sheet was gone again by the race intro (2026-10-11: something unloads sheets on the way
    // into a race; the game's own loader UnionSystemBP::SoundCuesheetLoadSync crashed when called from here). So
    // it is (re)loaded right before each race-music call for the custom stages' label (ensure_music_sheet), after
    // the race has loaded, and at start.
    // The game loads and unloads its cue sheets through UnionSystemBP (called from Blueprints while a race loads).
    // After each such call the custom music sheet is checked and loaded again if it was dropped: seconds before
    // the race intro, so its audio is ready when the intro jingle starts (reloaded only at the intro call, the
    // jingle was silent, 2026-10-11).
    auto watch_cue_sheet_loads() -> void
    {
        for (const CharType* fn : {STR("SoundCuesheetLoadSync"), STR("SoundCuesheetLoadAsync"), STR("SoundCuesheetUnloadSync")})
        {
            auto path = StringType(STR("/Script/UnionSystem.UnionSystemBP:")) + fn;
            auto* function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path.c_str());
            if (!function) continue;
            function->RegisterPostHook([function](UnrealScriptFunctionCallableContext& context, void*) {
                int32_t count = -1;
                if (context.TheStack.Locals())
                {
                    for (FProperty* p : function->ForEachProperty())
                    {
                        if (p->GetName() == STR("CuesheetArray")) count = p->ContainerPtrToValuePtr<RawArray<uint8_t>>(context.TheStack.Locals())->Num;
                    }
                }
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] game {} ({} sheets); {}\n"), function->GetName(), count, sheet_state(STR("BGM_ASR3001")));
                if (g_game_ok) ensure_music_sheet();
            });
        }
    }

    auto load_music_sheet() -> void
    {
        auto* cdo = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/CriWareRuntime.Default__SoundAtomCueSheet"));
        g_music_sheet = load_asset(STR("/Game/01_Union/Sound/Cue/BGM_ASR3001"), STR("BGM_ASR3001"));
        if (!cdo || !g_music_sheet) return;
        FnCall load(STR("/Script/CriWareRuntime.SoundAtomCueSheet:LoadAtomCueSheet"));
        load.set<UObject*>(STR("CueSheet"), g_music_sheet);
        load.set<bool>(STR("bAddToLevel"), false);
        load.call(cdo);
        FnCall make_sure(STR("/Script/CriWareRuntime.SoundAtomCueSheet:MakeSureCueSheetIsLoaded"));
        make_sure.call(g_music_sheet);
        // The root-set flag alone did not keep it (gone again within seconds, even mid-intro, 2026-10-11): it is
        // also marked standalone and held by a real reference the garbage collector follows, the engine's
        // GameSingleton (a UPROPERTY of the engine, unused by this game: only taken while empty).
        g_music_sheet->SetFlags(RF_Standalone);
        StringType holder = STR("none");
        if (auto* engine = UObjectGlobals::FindFirstOf(STR("GameEngine")))
        {
            if (auto* singleton = find_property(engine, STR("GameSingleton")))
            {
                auto** slot = singleton->ContainerPtrToValuePtr<UObject*>(engine);
                if (!*slot || (*slot)->GetName() == STR("BGM_ASR3001"))
                {
                    *slot = g_music_sheet;
                    holder = STR("GameEngine.GameSingleton");
                }
                else
                {
                    holder = StringType(STR("not used: GameSingleton holds ")) + (*slot)->GetName();
                }
            }
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] music sheet loaded: {}; held by {}\n"), sheet_state(STR("BGM_ASR3001")), holder);
    }

    auto ensure_music_sheet() -> void
    {
        std::vector<UObject*> sheets;
        UObjectGlobals::FindAllOf(STR("SoundAtomCueSheet"), sheets);
        for (auto* sheet : sheets)
        {
            if (sheet && sheet->GetName() == STR("BGM_ASR3001") && sheet_loaded(sheet)) return;
        }
        load_music_sheet();
    }

    // Tables the game loads only when they are needed (the podium scene) and assets the mod uses: loaded once, at
    // the first cup list, and kept.
    bool g_preloaded = false;

    auto preload() -> void
    {
        if (g_preloaded || !g_game_ok) return;
        g_preloaded = true;
        load_asset(STR("/Game/01_Union/Database/Scene/DT_CeremonySceneData01"), STR("DT_CeremonySceneData01"));
        load_asset(STR("/Game/01_Union/Database/Scene/CDT_CeremonySceneData"), STR("CDT_CeremonySceneData"));
        load_asset(STR("/Game/01_Union/Scene/Common/StageCameraSettings/DT_StageCameraSettings_Ceremony01"), STR("DT_StageCameraSettings_Ceremony01"));
        load_asset(STR("/Game/01_Union/Scene/Common/StageCameraSettings/CDT_StageCameraSettings_Ceremony"), STR("CDT_StageCameraSettings_Ceremony"));
        load_music_sheet();

    }

    // The borrowed ids must still mean what they meant for game 1.4.3; after a game update that renumbers them,
    // the mod stays out of the way (logged).
    auto enum_value(const CharType* enum_path, const CharType* name) -> int64_t
    {
        auto* e = UObjectGlobals::StaticFindObject<UEnum*>(nullptr, nullptr, enum_path);
        if (!e) return -1;
        for (auto pair : e->ForEachName())
        {
            if (pair.Key.ToString() == name) return pair.Value;
        }
        return -1;
    }

    auto check_game_version() -> void
    {
        struct Expect { const CharType* path; const CharType* name; int64_t value; };
        const Expect expected[] = {{STR("/Script/UnionSystem.EStageId"), STR("EStageId::STG1016"), 15},
                                   {STR("/Script/UnionSystem.EStageId"), STR("EStageId::STG1022"), 21},
                                   {STR("/Script/UnionSystem.EStageId"), STR("EStageId::STG3001"), 74},
                                   {STR("/Script/UnionSystem.EStageId"), STR("EStageId::STG3003"), 76},
                                   {STR("/Script/UnionSystem.EGrandPrixId"), STR("EGrandPrixId::Collabo_03"), 13}};
        for (auto& e : expected)
        {
            auto got = enum_value(e.path, e.name);
            if (got != e.value)
            {
                g_game_ok = false;
                Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] {} is {} (expected {}): game changed, mod disabled\n"), e.name, got, e.value);
            }
        }
        if (g_game_ok) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] stage / cup ids as expected\n"));
    }

    auto hooked_calc_selectable(GrandPrixIdArray* result) -> GrandPrixIdArray*
    {
        auto ret = reinterpret_cast<CalcSelectableFn>(g_trampoline)(result);
        ++g_calls;
        patch_stage_rows();
        preload();
        clone_stage_rows();
        if (ret && ret->Num >= 0 && ret->Num < 64 && cup_active() && append_cup(ret, kExtraCup))
        {
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] added cup {} to the list\n"), kExtraCup);
        }
        StringType ids;
        if (ret && ret->Data && ret->Num >= 0 && ret->Num <= 64)
        {
            for (int32_t i = 0; i < ret->Num; ++i)
            {
                ids += (i ? STR(", ") : STR("")) + std::to_wstring(ret->Data[i]);
            }
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] CalcSelectableGrandPrixIds call {}: {} cups [{}]\n"), g_calls,
                                        ret ? ret->Num : -1, ids);
        return ret;
    }

    // UWidget layout: its panel slot (and, for a canvas, the slot's FAnchorData: FMargin Offsets = 4 floats, then
    // FAnchors Minimum/Maximum and FVector2D Alignment = doubles in UE 5.4) and its render transform.
    auto dump_layout(UObject* widget) -> void
    {
        auto** slot_ptr = widget->GetValuePtrByPropertyNameInChain<UObject*>(STR("Slot"));
        UObject* slot = slot_ptr ? *slot_ptr : nullptr;
        if (!slot)
        {
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]       no slot\n"));
            return;
        }
        auto** parent_ptr = slot->GetValuePtrByPropertyNameInChain<UObject*>(STR("Parent"));
        UObject* parent = parent_ptr ? *parent_ptr : nullptr;
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]       slot {} in {} ({})\n"), slot->GetClassPrivate()->GetName(),
                                        parent ? parent->GetName() : STR("?"), parent ? parent->GetClassPrivate()->GetName() : STR("?"));
        if (auto* layout = slot->GetValuePtrByPropertyNameInChain<uint8_t>(STR("LayoutData")))
        {
            auto* f = reinterpret_cast<float*>(layout);
            auto* d = reinterpret_cast<double*>(layout + 16);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]       offsets ({}, {}, {}, {}) anchors ({}, {})-({}, {}) alignment ({}, {})\n"),
                                            f[0], f[1], f[2], f[3], d[0], d[1], d[2], d[3], d[4], d[5]);
        }
        if (auto* rt = widget->GetValuePtrByPropertyNameInChain<double>(STR("RenderTransform")))
        {
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]       render translation ({}, {}) scale ({}, {})\n"), rt[0], rt[1], rt[2], rt[3]);
        }
    }

    // Logs every reflected property (up to 64 bytes) whose raw value differs between two widgets of the same
    // class: designer-set values the runtime-created emblem lacks.
    auto diff_properties(UObject* a, UObject* b) -> void
    {
        if (!a || !b || a->GetClassPrivate() != b->GetClassPrivate()) return;
        int differing = 0;
        for (FProperty* p : a->GetClassPrivate()->ForEachPropertyInChain())
        {
            auto size = p->GetSize();
            if (size <= 0 || size > 64) continue;
            auto* va = p->ContainerPtrToValuePtr<uint8_t>(a);
            auto* vb = p->ContainerPtrToValuePtr<uint8_t>(b);
            if (std::memcmp(va, vb, static_cast<size_t>(size)) == 0) continue;
            ++differing;
            StringType hex_a, hex_b;
            for (int32_t i = 0; i < size && i < 16; ++i)
            {
                hex_a += std::format(STR("{:02X}"), va[i]);
                hex_b += std::format(STR("{:02X}"), vb[i]);
            }
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]     diff {} ({} B): designed {} / created {}\n"), p->GetName(), size, hex_a, hex_b);
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]     {} differing properties\n"), differing);
    }

    // Keys (and object values) of a TMap property: the set's sparse array is (Data, Num, Max) at the start of the
    // map; each element is the key/value pair followed by the hash links (layout from the property).
    auto dump_map(UObject* owner, const CharType* name) -> void
    {
        auto* property = CastField<FMapProperty>(find_property(owner, name));
        if (!property)
        {
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   {}: no map property\n"), name);
            return;
        }
        auto* map = property->ContainerPtrToValuePtr<uint8_t>(owner);
        auto& layout = property->GetMapLayout();
        auto* data = *reinterpret_cast<uint8_t**>(map);
        auto num = *reinterpret_cast<int32_t*>(map + 8);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   {}: map size {} B, {} elements, stride {}, value at +{}, hash next/index at +{}/+{}\n"),
                                        name, property->GetSize(), num, layout.SetLayout.Size, layout.ValueOffset,
                                        layout.SetLayout.HashNextIdOffset, layout.SetLayout.HashIndexOffset);
        if (!data || num < 0 || num > 64) return;
        bool object_values = CastField<FObjectProperty>(property->GetValueProp()) != nullptr;
        int32_t stage_ids_offset = -1;
        if (auto* value_struct = CastField<FStructProperty>(property->GetValueProp()))
        {
            for (FProperty* p : value_struct->GetStruct()->ForEachPropertyInChain())
            {
                if (p->GetName() == STR("StageId")) stage_ids_offset = p->GetOffset_Internal();
            }
        }
        StringType keys;
        for (int32_t i = 0; i < num; ++i)
        {
            uint8_t* element = data + static_cast<size_t>(i) * layout.SetLayout.Size;
            keys += std::format(STR("{}{}"), i ? STR(", ") : STR(""), static_cast<int>(element[0]));
            if (object_values)
            {
                auto* value = *reinterpret_cast<UObject**>(element + layout.ValueOffset);
                keys += std::format(STR("={}"), value ? value->GetName() : StringType(STR("null")));
            }
            if (stage_ids_offset >= 0)
            {
                auto* ids = reinterpret_cast<RawArray<uint8_t>*>(element + layout.ValueOffset + stage_ids_offset);
                keys += STR("[");
                for (int32_t k = 0; ids->Data && k < ids->Num && k < 8; ++k) keys += std::format(STR("{}{}"), k ? STR(" ") : STR(""), static_cast<int>(ids->Data[k]));
                keys += STR("]");
            }
            keys += std::format(STR("(h{},{})"), *reinterpret_cast<int32_t*>(element + layout.SetLayout.HashNextIdOffset),
                                *reinterpret_cast<int32_t*>(element + layout.SetLayout.HashIndexOffset));
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]    keys: {}\n"), keys);
    }

    // F8 (during a stuck loading screen): the race loader's progress flags and every streaming level's state.
    auto bool_property(UObject* object, const CharType* name) -> StringType
    {
        auto* p = CastField<FBoolProperty>(find_property(object, name));
        return p ? (p->GetPropertyValueInContainer(object) ? STR("1") : STR("0")) : STR("?");
    }

    auto object_property(UObject* object, const CharType* name) -> StringType
    {
        auto* p = find_property(object, name);
        if (!p || p->GetSize() != 8) return STR("?");
        auto* value = *p->ContainerPtrToValuePtr<UObject*>(object);
        return value ? value->GetName() : StringType(STR("null"));
    }

    // F8 also: the item boxes (respawning, 2026-10-11: taken boxes never came back on the custom stages).
    auto float_property(UObject* object, const CharType* name) -> StringType
    {
        auto* p = find_property(object, name);
        return p && p->GetSize() == 4 ? std::format(STR("{:.2f}"), *p->ContainerPtrToValuePtr<float>(object)) : StringType(STR("?"));
    }

    auto dump_item_boxes() -> void
    {
        std::vector<UObject*> boxes;
        UObjectGlobals::FindAllOf(STR("ItemBoxObject"), boxes);
        std::vector<UObject*> derived;
        UObjectGlobals::FindAllOf(STR("BP_ItemBox_C"), derived);
        boxes.insert(boxes.end(), derived.begin(), derived.end());
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] F8: {} item boxes\n"), boxes.size());
        int shown = 0;
        for (auto* box : boxes)
        {
            if (!box || shown >= 40) continue;
            auto* level = box->GetOuterPrivate();
            auto* world = level ? level->GetOuterPrivate() : nullptr;
            FnCall where(STR("/Script/Engine.Actor:K2_GetActorLocation"));
            double at[3]{};
            if (where.call(box)) std::memcpy(at, where.ptr(STR("ReturnValue")), sizeof(at));
            FnCall hidden(STR("/Script/Engine.Actor:IsHidden"));
            bool is_hidden = hidden.call(box) && hidden.get<bool>(STR("ReturnValue"));
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  {} in {} at ({:.0f}, {:.0f}, {:.0f}) hidden {} wait {} auto {} start {} disable {} enable {} event {}\n"),
                                            box->GetName(), world ? world->GetName() : StringType(STR("?")), at[0], at[1], at[2], is_hidden,
                                            float_property(box, STR("RelocationWaitTimeToNormal")), bool_property(box, STR("bIsAutoToggleEnable")),
                                            float_property(box, STR("AutoEnableStartTime")), float_property(box, STR("AutoDisableTime")),
                                            float_property(box, STR("AutoEnableTime")), bool_property(box, STR("UseEventStart")));
            ++shown;
        }
    }

    auto dump_loading() -> void
    {
        dump_item_boxes();
        std::vector<UObject*> loaders;
        UObjectGlobals::FindAllOf(STR("RaceSequenceStateLoadAssets"), loaders);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] F8: {} race loader(s)\n"), loaders.size());
        for (auto* l : loaders)
        {
            if (!l) continue;
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  {} stage {} driver {} parts {} effect {} stagePrimary {}\n"), l->GetName(),
                                            bool_property(l, STR("bLoadedStageAsset")), bool_property(l, STR("bLoadedDriverAsset")),
                                            bool_property(l, STR("bLoadedMachinePartsAsset")), bool_property(l, STR("bLoadedMachineEffectAsset")),
                                            bool_property(l, STR("bLoadedStagePrimaryAssets")));
            if (auto* assets = l->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("StageDataAssets")); assets && assets->Num >= 0 && assets->Num < 16)
            {
                for (int32_t i = 0; i < assets->Num; ++i)
                {
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   stage asset {}: {}\n"), i, assets->Data[i] ? assets->Data[i]->GetName() : StringType(STR("null")));
                }
            }
        }
        // the stage assets the loader waits for (FStageDynamicData): which ones are in memory
        for (const CharType* cls : {STR("UnionBtCollisionBinaryAsset"), STR("UnionNavigationBinaryDataAsset"),
                                    STR("UnionComPathBinaryDataAsset"), STR("UnionRunPathBinaryDataAsset"),
                                    STR("PrimaryStageDataAsset")})
        {
            std::vector<UObject*> objects;
            UObjectGlobals::FindAllOf(cls, objects);
            StringType names;
            for (auto* o : objects)
            {
                if (o) names += (names.empty() ? STR("") : STR(", ")) + o->GetName();
            }
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  {} ({}): {}\n"), cls, objects.size(), names);
        }
        for (const CharType* cls : {STR("World"), STR("Package")})
        {
            std::vector<UObject*> objects;
            UObjectGlobals::FindAllOf(cls, objects);
            StringType names;
            for (auto* o : objects)
            {
                if (!o) continue;
                auto name = o->GetName();
                if (name.find(STR("asr3001")) != StringType::npos || name.find(STR("stg1016")) != StringType::npos || name.find(STR("3001")) != StringType::npos)
                {
                    names += (names.empty() ? STR("") : STR(", ")) + name;
                }
            }
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  {} objects of ours / E-Stadium ({} total): {}\n"), cls, objects.size(), names);
        }
        std::vector<UObject*> textures;
        UObjectGlobals::FindAllOf(STR("Texture2D"), textures);
        StringType ours;
        for (auto* t : textures)
        {
            if (!t) continue;
            auto name = t->GetName();
            if (name.find(STR("3001")) != StringType::npos) ours += (ours.empty() ? STR("") : STR(", ")) + name;
        }
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  textures with 3001: {}\n"), ours);
        std::vector<UObject*> levels;
        UObjectGlobals::FindAllOf(STR("LevelStreaming"), levels);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  {} streaming level(s)\n"), levels.size());
        for (auto* level : levels)
        {
            if (!level) continue;
            StringType package = STR("?");
            if (auto* name = level->GetValuePtrByPropertyNameInChain<FName>(STR("PackageNameToLoad")); name && name->GetComparisonIndex())
            {
                package = name->ToString();
            }
            if (auto* world = find_property(level, STR("WorldAsset")); world && world->GetSize() == 40)
            {
                package = reinterpret_cast<FName*>(world->ContainerPtrToValuePtr<uint8_t>(level) + 8)->ToString() + STR(" (") + package + STR(")");
            }
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   {} loaded={} shouldLoad={} shouldShow={} {}\n"), level->GetClassPrivate()->GetName(),
                                            object_property(level, STR("LoadedLevel")), bool_property(level, STR("bShouldBeLoaded")),
                                            bool_property(level, STR("bShouldBeVisible")), package);
        }
    }

    // F6: what the course-select widget kept from the cup list (read only).
    auto dump_course_select() -> void
    {
        std::vector<UObject*> widgets;
        UObjectGlobals::FindAllOf(STR("CourseSelectWidgetBase"), widgets);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] F6: {} CourseSelectWidgetBase instance(s)\n"), widgets.size());
        for (auto* w : widgets)
        {
            if (!w) continue;
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  widget {}\n"), w->GetFullName());
            if (auto* ids = w->GetValuePtrByPropertyNameInChain<RawArray<uint8_t>>(STR("GrandPrixIds")); ids && ids->Num >= 0 && ids->Num < 64)
            {
                StringType list;
                for (int32_t i = 0; i < ids->Num; ++i) list += (i ? STR(", ") : STR("")) + std::to_wstring(ids->Data[i]);
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   GrandPrixIds ({}): [{}]\n"), ids->Num, list);
            }
            dump_map(w, STR("GrandPrixIdAndEmblemMap"));
            dump_map(w, STR("GrandPrixStagesMap"));
            if (auto* cur = w->GetValuePtrByPropertyNameInChain<uint8_t>(STR("CurrentSelectedGrandPrixId")))
            {
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   CurrentSelectedGrandPrixId {}\n"), *cur);
            }
            if (auto* emblems = w->GetValuePtrByPropertyNameInChain<RawArray<UObject*>>(STR("GrandPrixEmblemArr")); emblems && emblems->Num >= 0 && emblems->Num < 64)
            {
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   GrandPrixEmblemArr: {} slots\n"), emblems->Num);
                for (int32_t i = 0; i < emblems->Num; ++i)
                {
                    UObject* e = emblems->Data[i];
                    if (!e)
                    {
                        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]    [{}] null\n"), i);
                        continue;
                    }
                    auto* id = e->GetValuePtrByPropertyNameInChain<uint8_t>(STR("GrandPrixId"));
                    auto* panel = e->GetValuePtrByPropertyNameInChain<int32_t>(STR("PanelIndex"));
                    auto* button = e->GetValuePtrByPropertyNameInChain<int32_t>(STR("ButtonIndex"));
                    auto* focus = e->GetValuePtrByPropertyNameInChain<uint8_t>(STR("ButtonFocusType"));
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]    [{}] {} GrandPrixId {} PanelIndex {} ButtonIndex {} FocusType {}\n"), i, e->GetName(),
                                                    id ? static_cast<int>(*id) : -1, panel ? *panel : -999, button ? *button : -999,
                                                    focus ? static_cast<int>(*focus) : -1);
                    dump_layout(e);
                }
                if (emblems->Num == 11)
                {
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   property diff, designed {} vs created {}:\n"),
                                                    emblems->Data[9]->GetName(), emblems->Data[10]->GetName());
                    diff_properties(emblems->Data[9], emblems->Data[10]);
                }
            }
        }
    }

    struct ModuleRange
    {
        uintptr_t begin = 0, end = 0;
        bool contains(uintptr_t a) const { return a >= begin && a < end; }
    };

    auto main_module_range() -> ModuleRange
    {
        MODULEINFO info{};
        GetModuleInformation(GetCurrentProcess(), GetModuleHandleW(nullptr), &info, sizeof(info));
        auto base = reinterpret_cast<uintptr_t>(info.lpBaseOfDll);
        return {base, base + info.SizeOfImage};
    }

    // Disassembles the exec thunk up to its first RET, logs it, and returns the direct CALL/JMP targets in the
    // game module, in order.
    auto thunk_branch_targets(void* thunk, const ModuleRange& game) -> std::vector<uintptr_t>
    {
        ZydisDecoder decoder;
        ZydisDecoderInit(&decoder, ZYDIS_MACHINE_MODE_LONG_64, ZYDIS_STACK_WIDTH_64);
        ZydisFormatter formatter;
        ZydisFormatterInit(&formatter, ZYDIS_FORMATTER_STYLE_INTEL);

        std::vector<uintptr_t> targets;
        auto start = reinterpret_cast<uintptr_t>(thunk);
        size_t offset = 0;
        ZydisDecodedInstruction insn;
        ZydisDecodedOperand ops[ZYDIS_MAX_OPERAND_COUNT];
        while (offset < kMaxThunkBytes &&
               ZYAN_SUCCESS(ZydisDecoderDecodeFull(&decoder, reinterpret_cast<void*>(start + offset), kMaxThunkBytes - offset, &insn, ops)))
        {
            char text[256];
            ZydisFormatterFormatInstruction(&formatter, &insn, ops, insn.operand_count_visible, text, sizeof(text), start + offset, nullptr);
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]   +{:03X}  {}\n"), offset, widen(text));
            if ((insn.mnemonic == ZYDIS_MNEMONIC_CALL || insn.mnemonic == ZYDIS_MNEMONIC_JMP) && ops[0].type == ZYDIS_OPERAND_TYPE_IMMEDIATE)
            {
                ZyanU64 target = 0;
                if (ZYAN_SUCCESS(ZydisCalcAbsoluteAddress(&insn, &ops[0], start + offset, &target)) && game.contains(target))
                {
                    targets.push_back(static_cast<uintptr_t>(target));
                }
            }
            offset += insn.length;
            if (insn.mnemonic == ZYDIS_MNEMONIC_RET || (insn.mnemonic == ZYDIS_MNEMONIC_JMP && ops[0].type == ZYDIS_OPERAND_TYPE_IMMEDIATE))
            {
                break;  // end of the thunk (a tail JMP to the native function counts too)
            }
        }
        return targets;
    }

    // ASR's star balloons (BP_ASR_BalloonBreak_C, a native BreakObjectBase): on a hit the game hides the static
    // balloon and calls OnPlayBreakAnimation, where the Blueprint shows the skeletal mesh and plays ASR's pop.
    // In game the balloon just vanished (2026-10-11). Around each pop the mod logs what the game does with the
    // components, keeps the pop's mesh shown while it plays, adds a confetti burst (CrossWorlds' party cracker)
    // and grows the balloon back when the game puts it back (ASR's respawn).
    struct Balloon
    {
        FWeakObjectPtr actor;
        double popped_at = 0.0;
        double grow_from = -1.0;  // < 0: still popped
        bool logged_late = false;
        bool forced = false;
    };
    std::vector<Balloon> g_balloons;
    UObject* g_confetti = nullptr;
    bool g_confetti_tried = false;
    constexpr double kPopShow = 1.0;    // s the pop's mesh is kept shown
    constexpr double kLateLog = 0.3;    // s after the pop: the components' state is logged again
    constexpr double kGrowTime = 0.4;   // s of the regrow

    auto is_balloon(UObject* object) -> bool
    {
        return object && object->GetClassPrivate()->GetName().rfind(STR("BP_ASR_BalloonBreak"), 0) == 0;
    }

    auto component(UObject* actor, const CharType* name) -> UObject*
    {
        auto** slot = actor->GetValuePtrByPropertyNameInChain<UObject*>(name);
        return slot ? *slot : nullptr;
    }

    auto is_visible(UObject* comp) -> bool
    {
        FnCall vis(STR("/Script/Engine.SceneComponent:IsVisible"));
        return comp && vis.call(comp) && vis.get<bool>(STR("ReturnValue"));
    }

    auto set_visible(UObject* comp, bool visible) -> void
    {
        FnCall set(STR("/Script/Engine.SceneComponent:SetVisibility"));
        set.set<bool>(STR("bNewVisibility"), visible);
        set.set<bool>(STR("bPropagateToChildren"), true);
        set.call(comp);
    }

    auto balloon_state(UObject* actor) -> StringType
    {
        auto* skeletal = component(actor, STR("SkeletalMeshComponent"));
        auto* mesh = component(actor, STR("StaticMeshComponent"));
        FnCall hidden(STR("/Script/Engine.Actor:IsHidden"));
        FnCall broken(STR("/Script/UnionRun.BreakObjectBase:IsBroken"));
        FnCall position(STR("/Script/Engine.SkeletalMeshComponent:GetPosition"));
        FnCall playing(STR("/Script/Engine.SkeletalMeshComponent:IsPlaying"));
        bool actor_hidden = hidden.call(actor) && hidden.get<bool>(STR("ReturnValue"));
        bool is_broken = broken.call(actor) && broken.get<bool>(STR("ReturnValue"));
        float at = skeletal && position.call(skeletal) ? position.get<float>(STR("ReturnValue")) : -1.0f;
        bool plays = skeletal && playing.call(skeletal) && playing.get<bool>(STR("ReturnValue"));
        return std::format(STR("actor hidden {} broken {} | static visible {} hiddenInGame {} | skeletal visible {} hiddenInGame {} anim {:.2f} s playing {}"),
                           actor_hidden, is_broken, is_visible(mesh), mesh ? bool_property(mesh, STR("bHiddenInGame")) : STR("-"),
                           is_visible(skeletal), skeletal ? bool_property(skeletal, STR("bHiddenInGame")) : STR("-"), at, plays);
    }

    auto actor_location(UObject* actor, double out[3]) -> bool
    {
        FnCall where(STR("/Script/Engine.Actor:K2_GetActorLocation"));
        if (!where.call(actor)) return false;
        std::memcpy(out, where.ptr(STR("ReturnValue")), sizeof(double) * 3);
        return true;
    }

    auto set_actor_scale(UObject* actor, double scale) -> void
    {
        FnCall set(STR("/Script/Engine.Actor:SetActorScale3D"));
        double v[3] = {scale, scale, scale};
        set.set_raw(STR("NewScale3D"), v, sizeof(v));
        set.call(actor);
    }

    auto spawn_confetti(UObject* actor) -> void
    {
        if (!g_confetti_tried)
        {
            g_confetti_tried = true;
            g_confetti = load_asset(STR("/Game/01_Union/FX/Stage/stg2015/NS_FX_stg2015_PartyCracker"), STR("NS_FX_stg2015_PartyCracker"));
        }
        auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/Niagara.Default__NiagaraFunctionLibrary"));
        double at[3];
        if (!g_confetti || !library || !actor_location(actor, at)) return;
        FnCall spawn(STR("/Script/Niagara.NiagaraFunctionLibrary:SpawnSystemAtLocation"));
        double scale[3] = {1.0, 1.0, 1.0};
        spawn.set<UObject*>(STR("WorldContextObject"), actor);
        spawn.set<UObject*>(STR("SystemTemplate"), g_confetti);
        spawn.set_raw(STR("Location"), at, sizeof(at));
        spawn.set_raw(STR("Scale"), scale, sizeof(scale));
        spawn.set<bool>(STR("bAutoDestroy"), true);
        spawn.set<bool>(STR("bAutoActivate"), true);
        spawn.set<bool>(STR("bPreCullCheck"), true);
        spawn.call(library);
        auto* system = spawn.get<UObject*>(STR("ReturnValue"));
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon confetti: {}\n"), system ? system->GetName() : StringType(STR("not spawned")));
    }

    // ASR's pop sound: cue SE_ASR3001_BALLOON_POP of the custom stages' ambience sheet (pipeline.ambience; the
    // sheet object is SE_OBJ_STG1016, pointed at SE_OBJ_ASR3001 by redirect_sound), played at the balloon like
    // the stock break objects play theirs (UnionSystemBP: SoundBuildPlayer, SetLocation, Play). The cue object is
    // made once and kept; its sheet is looked up again for every pop.
    UObject* g_pop_cue = nullptr;
    bool g_pop_logged = false;

    auto ambience_sheet() -> UObject*
    {
        std::vector<UObject*> sheets;
        UObjectGlobals::FindAllOf(STR("SoundAtomCueSheet"), sheets);
        for (auto* sheet : sheets)
        {
            if (!sheet) continue;
            auto* name = sheet->GetValuePtrByPropertyNameInChain<FString>(STR("CueSheetName"));
            if (name && StringType(**name).find(STR("SE_OBJ_ASR3001")) != StringType::npos) return sheet;
        }
        return nullptr;
    }

    auto play_pop(UObject* actor) -> void
    {
        auto* sheet = ambience_sheet();
        auto* library = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Script/UnionSystem.Default__UnionSystemBP"));
        double at[3];
        if (!sheet || !library || !actor_location(actor, at))
        {
            if (!g_pop_logged) Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] balloon pop: no ambience sheet / sound library ({}; library {})\n"),
                                                              sheet_state(STR("SE_OBJ_STG1016")), library != nullptr);
            g_pop_logged = true;
            return;
        }
        if (!g_pop_cue)
        {
            auto* cls = UObjectGlobals::StaticFindObject<UClass*>(nullptr, nullptr, STR("/Script/CriWareRuntime.SoundAtomCue"));
            auto* outer = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, STR("/Engine/Transient"));
            if (!cls || !outer) return;
            FStaticConstructObjectParameters params(cls, outer);
            params.Name = FName(STR("ASR3001_BalloonPop"), FNAME_Add);
            g_pop_cue = UObjectGlobals::StaticConstructObject(params);
            if (!g_pop_cue) return;
            g_pop_cue->SetRootSet();
            if (auto* cue_name = g_pop_cue->GetValuePtrByPropertyNameInChain<FString>(STR("CueName"))) *cue_name = FString(STR("SE_ASR3001_BALLOON_POP"));
        }
        if (auto** cue_sheet = g_pop_cue->GetValuePtrByPropertyNameInChain<UObject*>(STR("CueSheet"))) *cue_sheet = sheet;
        FnCall build(STR("/Script/UnionSystem.UnionSystemBP:SoundBuildPlayer"));
        if (!build.call(library)) return;
        auto* handle_prop = build.prop(STR("ReturnValue"));
        if (!handle_prop) return;
        auto size = static_cast<size_t>(handle_prop->GetSize());
        FnCall place(STR("/Script/UnionSystem.UnionSystemBP:SetLocation"));
        place.set_raw(STR("Handle"), build.ptr(STR("ReturnValue")), size);
        place.set_raw(STR("Location"), at, sizeof(at));
        place.call(library);
        FnCall play(STR("/Script/UnionSystem.UnionSystemBP:Play"));
        play.set_raw(STR("Handle"), place.ptr(STR("ReturnValue")), size);
        play.set<UObject*>(STR("Sound"), g_pop_cue);
        play.call(library);
        if (!g_pop_logged) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon pop sound played ({})\n"), sheet->GetName());
        g_pop_logged = true;
    }

    // ASR's idle bob: the level gives each balloon's skeletal mesh its looping idle animation (ue_build_level), but
    // the break object shows only its static balloon until hit. From the race intro on, the mod shows the
    // skeletal mesh (bobbing) and hides the static one; a hit switches it to the pop (<label>_Hit_Anim), and when
    // the balloon returns it goes back to the loop. The collision box stays at the bottom of the bob.
    auto balloon_anim(UObject* skeletal, bool hit) -> UObject*
    {
        auto** mesh = skeletal->GetValuePtrByPropertyNameInChain<UObject*>(STR("SkeletalMeshAsset"));
        if (!mesh || !*mesh) mesh = skeletal->GetValuePtrByPropertyNameInChain<UObject*>(STR("SkeletalMesh"));
        if (!mesh || !*mesh) return nullptr;
        auto label = (*mesh)->GetName();  // Samba_Hard_Anim_t4_i1
        auto name = label + (hit ? STR("_Hit_Anim") : STR("_Anim"));
        auto package = StringType(STR("/Game/ASR/RockyCoaster/Animated/")) + label + (hit ? STR("/Hit/") : STR("/")) + name;
        if (auto* found = UObjectGlobals::StaticFindObject<UObject*>(nullptr, nullptr, (package + STR(".") + name).c_str())) return found;
        return load_asset(package.c_str(), name.c_str());
    }

    auto play_anim(UObject* skeletal, UObject* anim, bool loop) -> void
    {
        FnCall play(STR("/Script/Engine.SkeletalMeshComponent:PlayAnimation"));
        play.set<UObject*>(STR("NewAnimToPlay"), anim);
        play.set<bool>(STR("bLooping"), loop);
        play.call(skeletal);
    }

    // each balloon's place in its bob (the level staggers them: rows alternate), kept across pops: offset of the
    // loop's position from the mod's clock, and the loop's length
    struct BobPhase
    {
        FWeakObjectPtr actor;
        double offset = 0.0;
        double length = 0.0;
    };
    std::vector<BobPhase> g_bob_phases;
    double g_bob_check_at = -1.0;  // one check, a second after the balloons are shown: is the bob moving?
    FWeakObjectPtr g_bob_check;

    auto anim_position(UObject* skeletal) -> double
    {
        FnCall position(STR("/Script/Engine.SkeletalMeshComponent:GetPosition"));
        return position.call(skeletal) ? position.get<float>(STR("ReturnValue")) : 0.0;
    }

    auto anim_length(UObject* anim) -> double
    {
        // GetPlayLength returned 0 here (2026-10-11): the sequence's own length property first
        if (auto* length = anim ? anim->GetValuePtrByPropertyNameInChain<float>(STR("SequenceLength")) : nullptr)
        {
            if (*length > 0.0f) return *length;
        }
        FnCall length(STR("/Script/Engine.AnimSequenceBase:GetPlayLength"));
        return anim && length.call(anim) ? length.get<float>(STR("ReturnValue")) : 0.0;
    }

    auto show_idle(UObject* actor, bool restart) -> void
    {
        auto* skeletal = component(actor, STR("SkeletalMeshComponent"));
        auto* mesh = component(actor, STR("StaticMeshComponent"));
        if (!skeletal) return;
        if (mesh) set_visible(mesh, false);
        set_visible(skeletal, true);
        FnCall unhide(STR("/Script/Engine.SceneComponent:SetHiddenInGame"));
        unhide.set<bool>(STR("NewHidden"), false);
        unhide.set<bool>(STR("bPropagateToChildren"), true);
        unhide.call(skeletal);
        // the break object keeps the skeletal mesh's tick off until it breaks: the idle loop stood still
        FnCall tick(STR("/Script/Engine.ActorComponent:SetComponentTickEnabled"));
        tick.set<bool>(STR("bEnabled"), true);
        tick.call(skeletal);
        FnCall playing(STR("/Script/Engine.SkeletalMeshComponent:IsPlaying"));
        bool plays = playing.call(skeletal) && playing.get<bool>(STR("ReturnValue"));
        if (restart || !plays)
        {
            if (auto* idle = balloon_anim(skeletal, false))
            {
                play_anim(skeletal, idle, true);
                // back in step with its row
                auto it = std::find_if(g_bob_phases.begin(), g_bob_phases.end(), [&](auto& p) { return p.actor.Get() == actor; });
                if (it != g_bob_phases.end() && it->length > 0.0)
                {
                    FnCall seek(STR("/Script/Engine.SkeletalMeshComponent:SetPosition"));
                    seek.set<float>(STR("InPos"), static_cast<float>(std::fmod(g_clock + it->offset, it->length)));
                    seek.set<bool>(STR("bFireNotifies"), false);
                    seek.call(skeletal);
                }
            }
        }
    }

    auto init_balloons() -> void
    {
        std::vector<UObject*> balloons;
        UObjectGlobals::FindAllOf(STR("BP_ASR_BalloonBreak_C"), balloons);
        int shown = 0;
        for (auto* actor : balloons)
        {
            if (!actor) continue;
            show_idle(actor, false);
            if (auto* skeletal = component(actor, STR("SkeletalMeshComponent")))
            {
                double length = anim_length(balloon_anim(skeletal, false));
                double offset = length > 0.0 ? std::fmod(anim_position(skeletal) - g_clock, length) + length : 0.0;
                auto it = std::find_if(g_bob_phases.begin(), g_bob_phases.end(), [&](auto& p) { return p.actor.Get() == actor; });
                if (it == g_bob_phases.end()) g_bob_phases.push_back(BobPhase{FWeakObjectPtr(actor), offset, length});
                else *it = BobPhase{FWeakObjectPtr(actor), offset, length};
                auto* anim = balloon_anim(skeletal, false);
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix]  balloon {} ({}): bob at {:.2f} of {:.2f} s\n"), actor->GetName(),
                                                anim ? anim->GetName() : StringType(STR("no idle animation")), anim_position(skeletal), length);
            }
            ++shown;
        }
        g_bob_check_at = balloons.empty() ? -1.0 : g_clock + 1.0;
        g_bob_check = balloons.empty() ? FWeakObjectPtr() : FWeakObjectPtr(balloons[0]);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] {} star balloons bobbing{}\n"), shown,
                                        shown ? StringType(STR(": ")) + balloon_state(balloons[0]) : StringType());
    }

    auto on_balloon_event(UObject* actor, UFunction* function) -> void
    {
        if (function->GetName() == STR("OnPlayBreakAnimation"))
        {
            // the Blueprint restarted the current (idle) animation: the pop instead
            if (auto* skeletal = component(actor, STR("SkeletalMeshComponent")))
            {
                if (auto* hit = balloon_anim(skeletal, true)) play_anim(skeletal, hit, false);
            }
        }
        auto name = function->GetName();
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon {} {}: {}\n"), actor->GetName(), name, balloon_state(actor));
        if (name != STR("OnPlayBreakAnimation")) return;
        for (auto& b : g_balloons)
        {
            if (b.actor.Get() == actor)
            {
                b = Balloon{FWeakObjectPtr(actor), g_clock};
                spawn_confetti(actor);
                play_pop(actor);
                return;
            }
        }
        g_balloons.push_back(Balloon{FWeakObjectPtr(actor), g_clock});
        spawn_confetti(actor);
        play_pop(actor);
    }

    auto tick_balloons(double dt) -> void
    {
        g_clock += dt;
        static double redirect_next = 0.0;
        if (g_redirect_pending >= 0 && g_clock > g_redirect_until)
        {
            g_redirect_pending = -1;
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] ambience sheet never loaded: redirect given up\n"));
        }
        if (g_redirect_pending >= 0 && g_clock >= redirect_next)
        {
            redirect_next = g_clock + 0.1;
            redirect_sound(g_redirect_pending == 1, true);
        }
        if (g_bob_check_at >= 0.0 && g_clock >= g_bob_check_at)
        {
            g_bob_check_at = -1.0;
            if (auto* actor = g_bob_check.Get()) Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] bob after 1 s: {}\n"), balloon_state(actor));
        }
        if (g_balloons.empty()) return;
        for (size_t i = 0; i < g_balloons.size();)
        {
            auto& b = g_balloons[i];
            auto* actor = b.actor.Get();
            if (!actor)
            {
                g_balloons.erase(g_balloons.begin() + static_cast<std::ptrdiff_t>(i));
                continue;
            }
            double since = g_clock - b.popped_at;
            if (b.grow_from < 0.0)
            {
                auto* skeletal = component(actor, STR("SkeletalMeshComponent"));
                if (since < kPopShow && skeletal && !is_visible(skeletal))
                {
                    set_visible(skeletal, true);
                    if (!b.forced)
                    {
                        b.forced = true;
                        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon {}: pop mesh hidden at {:.2f} s, shown again\n"), actor->GetName(), since);
                    }
                }
                if (!b.logged_late && since >= kLateLog)
                {
                    b.logged_late = true;
                    Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon {} +{:.1f} s: {}\n"), actor->GetName(), since, balloon_state(actor));
                }
                if (since >= kPopShow)
                {
                    FnCall broken(STR("/Script/UnionRun.BreakObjectBase:IsBroken"));
                    if (broken.call(actor) && !broken.get<bool>(STR("ReturnValue")))
                    {
                        // put back by the game: grow from nothing
                        b.grow_from = g_clock;
                        set_actor_scale(actor, 0.01);
                        show_idle(actor, true);  // the game shows its static balloon again: back to the bobbing one
                        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon {} back after {:.2f} s: {}\n"), actor->GetName(), since, balloon_state(actor));
                    }
                }
            }
            else
            {
                double t = (std::min)(1.0, (g_clock - b.grow_from) / kGrowTime);
                // ease out with a small overshoot (a balloon inflating)
                constexpr double c1 = 1.70158, c3 = c1 + 1.0;
                double s = 1.0 + c3 * std::pow(t - 1.0, 3.0) + c1 * std::pow(t - 1.0, 2.0);
                set_actor_scale(actor, (std::max)(0.01, s));
                if (t >= 1.0)
                {
                    set_actor_scale(actor, 1.0);
                    g_balloons.erase(g_balloons.begin() + static_cast<std::ptrdiff_t>(i));
                    continue;
                }
            }
            ++i;
        }
    }

    // Item boxes taken on the custom stages never came back (2026-10-11). For the first boxes hit in a session
    // (ours and the stock travel-ring ones alike) every reflected property of the box and its components is
    // recorded just before the hit and compared afterwards: what changes when a box is taken, and whether it
    // changes back.
    struct BoxWatch
    {
        FWeakObjectPtr actor;
        double hit = 0.0;
        size_t next = 0;
        std::vector<std::pair<StringType, std::vector<uint8_t>>> before;
    };
    std::vector<BoxWatch> g_boxes;
    int g_boxes_watched = 0;
    constexpr int kMaxWatchedBoxes = 6;
    constexpr double kBoxChecks[] = {0.1, 1.0, 2.5, 5.0, 10.0};

    auto snapshot(UObject* object, const StringType& prefix, std::vector<std::pair<StringType, std::vector<uint8_t>>>& out, int depth) -> void
    {
        for (FProperty* p : object->GetClassPrivate()->ForEachPropertyInChain())
        {
            auto size = p->GetSize();
            if (size <= 0 || size > 64) continue;
            auto* v = p->ContainerPtrToValuePtr<uint8_t>(object);
            auto name = prefix + p->GetName();
            out.emplace_back(name, std::vector<uint8_t>(v, v + size));
            if (depth == 0 && CastField<FObjectProperty>(p))
            {
                auto* sub = *reinterpret_cast<UObject**>(v);
                if (sub && sub->GetOuterPrivate() == object) snapshot(sub, name + STR("."), out, 1);  // its components
            }
        }
    }

    auto box_world(UObject* box) -> StringType
    {
        auto* level = box->GetOuterPrivate();
        auto* world = level ? level->GetOuterPrivate() : nullptr;
        return world ? world->GetName() : StringType(STR("?"));
    }

    auto on_box_hit(UObject* box, UFunction* function) -> void
    {
        for (auto& w : g_boxes)
        {
            if (w.actor.Get() == box) return;  // already watched
        }
        if (g_boxes_watched >= kMaxWatchedBoxes) return;
        ++g_boxes_watched;
        BoxWatch w{FWeakObjectPtr(box), g_clock};
        snapshot(box, STR(""), w.before, 0);
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] item box {} in {}: {} ({} values recorded)\n"), box->GetName(), box_world(box),
                                        function->GetName(), w.before.size());
        g_boxes.push_back(std::move(w));
    }

    auto tick_boxes() -> void
    {
        for (size_t i = 0; i < g_boxes.size();)
        {
            auto& w = g_boxes[i];
            auto* box = w.actor.Get();
            if (!box || w.next >= std::size(kBoxChecks))
            {
                g_boxes.erase(g_boxes.begin() + static_cast<std::ptrdiff_t>(i));
                continue;
            }
            if (g_clock - w.hit >= kBoxChecks[w.next])
            {
                std::vector<std::pair<StringType, std::vector<uint8_t>>> now;
                snapshot(box, STR(""), now, 0);
                StringType changed;
                int count = 0;
                for (auto& [name, bytes] : now)
                {
                    auto it = std::find_if(w.before.begin(), w.before.end(), [&](auto& b) { return b.first == name; });
                    if (it == w.before.end() || it->second == bytes) continue;
                    if (++count > 24) continue;
                    StringType hex_a, hex_b;
                    for (size_t k = 0; k < bytes.size() && k < 12; ++k)
                    {
                        hex_a += std::format(STR("{:02X}"), it->second[k]);
                        hex_b += std::format(STR("{:02X}"), bytes[k]);
                    }
                    changed += std::format(STR(" {}: {} -> {};"), name, hex_a, hex_b);
                }
                Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] item box {} +{:.1f} s: {} changed{}\n"), box->GetName(), kBoxChecks[w.next], count, changed);
                ++w.next;
            }
            ++i;
        }
    }

    auto watch_balloons() -> void
    {
        static const FName events[] = {FName(STR("OnPlayBreakAnimation"), FNAME_Add), FName(STR("OnPlayBreakSound"), FNAME_Add),
                                       FName(STR("OnPlayClashSound"), FNAME_Add)};
        static const FName countdown[] = {FName(STR("OnReadyBeforeCountDown"), FNAME_Add), FName(STR("NoticeChangeCountDownEvent"), FNAME_Add)};
        Hook::RegisterProcessEventPostCallback(
            [](Hook::TCallbackIterationData<void>&, UObject* context, UFunction* function, void*) {
                if (!function || !context) return;
                auto name = function->GetNamePrivate();
                if (g_lights_turned)
                {
                    // the countdown begins: the intro cameras are done, the lights turn back (at the race start,
                    // PlayFirstLap, the countdown's lit objects visibly changed, 2026-10-11; OnReadyBeforeCountDown
                    // never came through here)
                    if (name == countdown[0] || name == countdown[1])
                    {
                        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] countdown ({} on {}) {:.1f} s after the intro\n"), function->GetName(),
                                                        context->GetName(), (GetTickCount64() - g_intro_at) / 1000.0);
                        turn_level_lights(false);
                        return;
                    }
                    // what the game calls between the intro and the countdown (first call of each function)
                    static std::vector<UFunction*> seen;
                    if (seen.size() < 400 && std::find(seen.begin(), seen.end(), function) == seen.end())
                    {
                        seen.push_back(function);
                        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] intro +{:.1f} s: {}.{} on {}\n"), (GetTickCount64() - g_intro_at) / 1000.0,
                                                        context->GetClassPrivate()->GetName(), function->GetName(), context->GetName());
                    }
                }
                if (name != events[0] && name != events[1] && name != events[2]) return;
                if (is_balloon(context)) on_balloon_event(context, function);
            },
            {false, false, STR("CustomGrandPrix"), STR("BalloonEvents")});
        static const FName box_events[] = {FName(STR("OnHitRacer"), FNAME_Add), FName(STR("OnPlayTakeSound"), FNAME_Add)};
        Hook::RegisterProcessEventPreCallback(
            [](Hook::TCallbackIterationData<void>&, UObject* context, UFunction* function, void*) {
                if (!function || !context) return;
                auto name = function->GetNamePrivate();
                if (name != box_events[0] && name != box_events[1]) return;
                if (context->GetClassPrivate()->GetName() == STR("BP_ItemBox_C")) on_box_hit(context, function);
            },
            {false, false, STR("CustomGrandPrix"), STR("ItemBoxEvents")});
        Hook::RegisterEngineTickPostCallback(
            [](Hook::TCallbackIterationData<void>&, UEngine*, float dt, bool) {
                tick_balloons(dt);
                if (!g_boxes.empty()) tick_boxes();
            },
            {false, false, STR("CustomGrandPrix"), STR("BalloonTick")});
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] balloon hooks registered\n"));
    }
}

class CustomGrandPrix : public CppUserModBase
{
    std::unique_ptr<PLH::x64Detour> m_detour;

public:
    CustomGrandPrix() : CppUserModBase()
    {
        ModName = STR("CustomGrandPrix");
        ModVersion = STR("0.45");
        ModDescription = STR("Custom Grand Prix for Sonic Racing CrossWorlds (offline Grand Prix only)");
        ModAuthors = STR("AllStarsRacingImporter");
    }

    ~CustomGrandPrix() override
    {
        if (m_detour) m_detour->unHook();
    }

    auto on_unreal_init() -> void override
    {
        check_game_version();
        register_keydown_event(Input::Key::F6, [] { dump_course_select(); });
        register_keydown_event(Input::Key::F8, [] { dump_loading(); });
        register_keydown_event(Input::Key::F10, [] {
            g_add_cup = !g_add_cup;
            if (g_add_cup) patch_stage_rows();
            if (g_add_cup) clone_stage_rows();
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] F10: extra cup {} {}\n"), kExtraCup,
                                            g_add_cup ? STR("ON (open the Grand Prix menu now)") : STR("OFF"));
        });
        if (auto* per_emblem = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, STR("/Script/UNION.CourseSelectWidgetBase:IsStoreRequiredForGrandPrix")))
        {
            per_emblem->RegisterPreHook([](UnrealScriptFunctionCallableContext& context, void*) {
                if (cup_active()) add_emblem_slot(context.Context);
            });
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] pre-hook on IsStoreRequiredForGrandPrix registered\n"));
        }
        replace_first_track();
        for (const CharType* path : {STR("/Script/UNION.CourseSelectSubCategoryIcon:OnSelected"), STR("/Script/UNION.CourseSelectSubCategoryIcon:OnDeselected")})
        {
            if (auto* focus_change = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, path))
            {
                focus_change->RegisterPostHook([](UnrealScriptFunctionCallableContext&, void*) {
                    if (cup_active()) relink_new_emblems();
                });
            }
        }
        override_for_extra_cup(STR("/Script/UNION.GrandPrixUtilityLibrary:IsGrandPrixOpened"), true);
        override_for_extra_cup(STR("/Script/UNION.CourseSelectWidgetBase:IsStoreRequiredForGrandPrix"), false);
        allow_custom_stages(STR("/Script/UnionSystem.UnionContentUtils:CanUseStageByGameModeForCourseSelect"));
        allow_custom_stages(STR("/Script/UnionSystem.UnionContentUtils:CanUseStageByGameMode"));
        allow_custom_stages(STR("/Script/UNION.GrandPrixUtilityLibrary:CanUseStageByStreamingInstallStatus"));
        log_music_calls();
        watch_cue_sheet_loads();
        watch_balloons();
        auto function = UObjectGlobals::StaticFindObject<UFunction*>(nullptr, nullptr, kFunctionPath);
        if (!function)
        {
            Output::send<LogLevel::Error>(STR("[CustomGrandPrix] {} not found; nothing hooked\n"), kFunctionPath);
            return;
        }
        // Another mod's Lua RegisterHook (GPRecon) swaps the UFunction's pointer for UE4SS's wrapper: use the
        // original exec thunk it keeps.
        void* thunk = reinterpret_cast<void*>(function->GetFuncPtr());
        auto& hooked = Internal::GetHookedFunctionsMap();
        if (auto it = hooked.find(function); it != hooked.end())
        {
            thunk = reinterpret_cast<void*>(it->second.GetOriginalFuncPtr());
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] function is Lua-hooked; using the original thunk\n"));
        }
        auto game = main_module_range();
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] exec thunk at game+0x{:X}:\n"), reinterpret_cast<uintptr_t>(thunk) - game.begin);
        if (!game.contains(reinterpret_cast<uintptr_t>(thunk)))
        {
            Output::send<LogLevel::Error>(STR("[CustomGrandPrix] thunk is outside the game module; nothing hooked\n"));
            return;
        }
        auto targets = thunk_branch_targets(thunk, game);
        for (auto t : targets)
        {
            Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] branch target game+0x{:X}\n"), t - game.begin);
        }
        // A parameterless static UFUNCTION returning a TArray: the thunk advances the bytecode pointer (P_FINISH),
        // calls the native function into a temporary, moves it into the result and frees the old and the
        // temporary allocations (game 1.4.3: call native, then FMemory::Free twice). So the native function is
        // the FIRST call, and every later call must go to one single other function (the free). Anything
        // else: log only, hook nothing.
        bool shape_ok = !targets.empty();
        for (size_t i = 2; i < targets.size(); ++i)
        {
            shape_ok = shape_ok && targets[i] == targets[1];
        }
        shape_ok = shape_ok && (targets.size() < 2 || targets[1] != targets[0]);
        if (!shape_ok)
        {
            Output::send<LogLevel::Warning>(STR("[CustomGrandPrix] unexpected thunk shape ({} branch targets); nothing hooked\n"), targets.size());
            return;
        }
        m_detour = std::make_unique<PLH::x64Detour>(static_cast<uint64_t>(targets[0]),
                                                    reinterpret_cast<uint64_t>(&hooked_calc_selectable), &g_trampoline);
        bool ok = m_detour->hook();
        Output::send<LogLevel::Verbose>(STR("[CustomGrandPrix] detour on game+0x{:X}: {}\n"), targets[0] - game.begin,
                                        ok ? STR("installed") : STR("FAILED"));
        if (!ok) m_detour.reset();
    }
};

#define CUSTOM_GRAND_PRIX_API __declspec(dllexport)
extern "C"
{
    CUSTOM_GRAND_PRIX_API CppUserModBase* start_mod()
    {
        return new CustomGrandPrix();
    }

    CUSTOM_GRAND_PRIX_API void uninstall_mod(CppUserModBase* mod)
    {
        delete mod;
    }
}
