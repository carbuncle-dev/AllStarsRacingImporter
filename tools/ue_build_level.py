"""Unreal Editor Python: build a CrossWorlds stage level from an exported ASR track.

Run in a windowless full editor (see pipeline/ue_build.py) -- the pythonscript commandlet has no
editor world, so spawning actors crashes there:
    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript="tools/ue_build_level.py" -unattended -nullrhi
(the config path is passed through the UE_BUILD_CONFIG environment variable; the editor quits when done)

config.json:
    fbx            exported visuals (tools/export_for_unreal.py), already in stage coordinates
    materials      materials.json: material name -> {texture, masked, kind, ...} (pipeline/unreal_materials.py)
    vertex_lighting  apply ASR's baked vertex colours to opaque/masked scenery (default off)
    content_root   e.g. /Game/ASR/RockyCoaster  (new assets go here)
    level_path     e.g. /Game/01_Union/Map/Stage_NotNX/stg1016/SL_stg1016  (replaces the stage's visual level)
    unlit_match    substrings of material names rendered unlit (sky domes)
"""

import json
import math
import os
import re
import zlib

import unreal

cfg = json.load(open(os.environ["UE_BUILD_CONFIG"]))
ROOT = cfg["content_root"]
tools = unreal.AssetToolsHelpers.get_asset_tools()
mel = unreal.MaterialEditingLibrary
eal = unreal.EditorAssetLibrary


def log(msg):
    unreal.log(f"[ASR] {msg}")


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def import_task(filename, dest, options=None, name=None, factory=None):
    t = unreal.AssetImportTask()
    if factory:
        t.factory = factory
    t.filename = filename
    t.destination_path = dest
    t.automated = True
    t.replace_existing = True
    t.save = False
    if name:
        t.destination_name = name
    if options:
        t.options = options
    return t


# Previous outputs are deleted on disk by pipeline/ue_build.py before the editor starts:
# deleting a loaded World asset from Python crashes this engine build.

# --- Textures -----------------------------------------------------------------
materials = json.load(open(cfg["materials"]))
textures = sorted({p for m in materials.values() for p in (m["texture"], m.get("ripple"), m.get("env"), m.get("layer2"), m.get("sparkle"), m.get("glow")) if p})
tex_names = {p: "T_" + re.sub(r"\W", "_", os.path.splitext(os.path.basename(p))[0]) for p in textures}
# The classic TextureFactory imports synchronously; Interchange finishes asynchronously, so most
# textures weren't ready (or saved) by the time materials referenced them.
tools.import_asset_tasks([import_task(p, f"{ROOT}/Textures", name=n, factory=unreal.TextureFactory())
                          for p, n in tex_names.items()])
tex_assets = {}
# UE auto-detects mostly-blue textures as normal maps (two-channel compression, no sRGB): the speakers'
# light-blue sound wave (SOUND_WAVE) came out lime green in game. Only the water's ripple maps are
# normal maps here; everything else is colour.
ripples = {m.get("ripple") for m in materials.values() if m.get("ripple")}
for p, n in tex_names.items():
    asset = unreal.load_asset(f"{ROOT}/Textures/{n}")
    if asset:
        if p not in ripples and asset.get_editor_property("compression_settings") == unreal.TextureCompressionSettings.TC_NORMALMAP:
            asset.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_DEFAULT)
            asset.set_editor_property("srgb", True)
            log(f"texture {n}: auto-detected as a normal map, set back to colour")
        tex_assets[p] = asset
# Our meshes have no texture-streaming data, so the game kept them at low mips; load full res.
for tex in tex_assets.values():
    tex.set_editor_property("never_stream", True)
log(f"imported {len(tex_assets)}/{len(textures)} textures")


# --- Master materials ---------------------------------------------------------
UNLIT = unreal.MaterialShadingModel.MSM_UNLIT
EMISSIVE = unreal.MaterialProperty.MP_EMISSIVE_COLOR


def expr(mat, cls, x, y, **props):
    e = mel.create_material_expression(mat, cls, x, y)
    for k, v in props.items():
        e.set_editor_property(k, v)
    return e


def link(a, a_out, b, b_in):
    mel.connect_material_expressions(a, a_out, b, b_in)


def binary(mat, cls, a, b, x, y, a_out="", b_out=""):
    e = expr(mat, cls, x, y)
    link(a, a_out, e, "A")
    link(b, b_out, e, "B")
    return e


def vertex_colour(mat):
    """ASR's vertex colour (rgb, alpha expressions), carried exactly in UV channels 1 (r, g) and 2 (b, a)
    by export_for_unreal. UE flips V on FBX import, so g and a are 1 - v."""
    rg = expr(mat, unreal.MaterialExpressionTextureCoordinate, -1400, 600, coordinate_index=1)
    ba = expr(mat, unreal.MaterialExpressionTextureCoordinate, -1400, 750, coordinate_index=2)
    r = expr(mat, unreal.MaterialExpressionComponentMask, -1200, 550, r=True, g=False, b=False, a=False)
    link(rg, "", r, "")
    g = expr(mat, unreal.MaterialExpressionComponentMask, -1200, 650, r=False, g=True, b=False, a=False)
    link(rg, "", g, "")
    b = expr(mat, unreal.MaterialExpressionComponentMask, -1200, 750, r=True, g=False, b=False, a=False)
    link(ba, "", b, "")
    a = expr(mat, unreal.MaterialExpressionComponentMask, -1200, 850, r=False, g=True, b=False, a=False)
    link(ba, "", a, "")
    g1 = expr(mat, unreal.MaterialExpressionOneMinus, -1050, 650)
    link(g, "", g1, "")
    a1 = expr(mat, unreal.MaterialExpressionOneMinus, -1050, 850)
    link(a, "", a1, "")
    rgb = binary(mat, unreal.MaterialExpressionAppendVector,
                 binary(mat, unreal.MaterialExpressionAppendVector, r, g1, -900, 600), b, -750, 650)
    return rgb, a1


def asr_lighting(mat, rgb):
    """ASR's baked lighting factor: texture * 2 * vertex colour in gamma space = (2 * vc)^2.2 in linear."""
    scale = expr(mat, unreal.MaterialExpressionScalarParameter, -700, 500, parameter_name="VertexColorScale",
                 default_value=2.0)
    gamma = expr(mat, unreal.MaterialExpressionScalarParameter, -700, 650, parameter_name="VertexColorGamma",
                 default_value=2.2)
    power = expr(mat, unreal.MaterialExpressionPower, -450, 550)
    link(binary(mat, unreal.MaterialExpressionMultiply, rgb, scale, -550, 550), "", power, "Base")
    link(gamma, "", power, "Exp")
    return power


# UE's skinned-mesh vertex factory takes at most 4 UV channels (GpuSkinVertexFactory.ush); a material
# reading UV4/UV5 and flagged for skeletal meshes fails to compile for every mesh (the default grey
# material is used instead). So each master comes twice: static (all channels) and "_Skin" (UV0-UV3:
# texture, vertex colour, pan; no flipbook) for the animated trees.
SKINNED = False
LAST_FLIP = None  # flipbook offset node of the material being built (texture_uv)


def new_material(name, blend, shading, two_sided):
    if SKINNED:
        name += "_Skin"
    path = f"{ROOT}/Materials/{name}"
    if eal.does_asset_exist(path):
        eal.delete_asset(path)
    mat = tools.create_asset(name, f"{ROOT}/Materials", unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property("blend_mode", blend)
    mat.set_editor_property("shading_model", shading)
    mat.set_editor_property("two_sided", two_sided)
    mat.set_editor_property("used_with_skeletal_mesh", SKINNED)
    return mat


def texture_uv(mat):
    """UV0 plus ASR's per-piece texture animation (export_for_unreal): "Pan" (UV3) = scroll rate (u, v)
    per s; "Flip1" (UV4) = (period s, phase), "Flip2" (UV5) = (columns, rows) of a flipbook (zero: none).
    UE flips V on import, so each pair arrives as (x, 1 - y) and offsets along V are negated.
    Skinned masters (see SKINNED) only pan."""
    C = unreal.MaterialExpressionConstant
    one2 = expr(mat, unreal.MaterialExpressionConstant2Vector, -2200, -400, r=0.0, g=1.0)
    time = expr(mat, unreal.MaterialExpressionTime, -2200, -300)
    pan = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, -500, coordinate_index=3)
    scroll = binary(mat, unreal.MaterialExpressionMultiply,
                    binary(mat, unreal.MaterialExpressionSubtract, pan, one2, -2050, -500), time, -1900, -500)
    uv0 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -950, -300, coordinate_index=0)
    if SKINNED:
        return binary(mat, unreal.MaterialExpressionAdd, uv0, scroll, -800, -350)

    def comp(e, ch, x, y):
        m = expr(mat, unreal.MaterialExpressionComponentMask, x, y, r=ch == "r", g=ch == "g", b=False, a=False)
        link(e, "", m, "")
        return m

    def one_minus(e, x, y):
        o = expr(mat, unreal.MaterialExpressionOneMinus, x, y)
        link(e, "", o, "")
        return o

    def unary(cls, e, x, y):
        o = expr(mat, cls, x, y)
        link(e, "", o, "")
        return o

    f1 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, -150, coordinate_index=4)
    f2 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, 50, coordinate_index=5)
    period = binary(mat, unreal.MaterialExpressionMax, comp(f1, "r", -2050, -200),
                    expr(mat, C, -2050, -120, r=0.001), -1900, -180)
    phase = one_minus(comp(f1, "g", -2050, -80), -1950, -80)
    nu_raw = comp(f2, "r", -2050, 20)
    nu = binary(mat, unreal.MaterialExpressionMax, nu_raw, expr(mat, C, -2050, 100, r=1.0), -1900, 40)
    # rows: integer part; the fractional part x 2 = share of each step spent sliding to the next row (the
    # billboard's screen; 0 = an instant switch)
    nv_raw = one_minus(comp(f2, "g", -2050, 160), -1950, 160)
    nv = binary(mat, unreal.MaterialExpressionMax, unary(unreal.MaterialExpressionFloor, nv_raw, -1950, 200),
                expr(mat, C, -2050, 240, r=1.0), -1900, 180)
    slide = binary(mat, unreal.MaterialExpressionMultiply, unary(unreal.MaterialExpressionFrac, nv_raw, -1950, 300),
                   expr(mat, C, -2050, 340, r=2.0), -1850, 300)
    steps = binary(mat, unreal.MaterialExpressionAdd,
                   binary(mat, unreal.MaterialExpressionDivide, time, period, -1750, -250), phase, -1650, -200)
    n = unary(unreal.MaterialExpressionFloor, steps, -1550, -200)
    # eased slide over the last `slide` of the step: s = smoothstep(saturate((frac - 1 + slide) / slide))
    into = binary(mat, unreal.MaterialExpressionDivide,
                  binary(mat, unreal.MaterialExpressionAdd,
                         binary(mat, unreal.MaterialExpressionSubtract, unary(unreal.MaterialExpressionFrac, steps, -1550, 300),
                                expr(mat, C, -1550, 380, r=1.0), -1450, 320), slide, -1350, 320),
                  binary(mat, unreal.MaterialExpressionMax, slide, expr(mat, C, -1450, 440, r=0.001), -1350, 420), -1250, 340)
    s = unary(unreal.MaterialExpressionSaturate, into, -1150, 340)
    eased = binary(mat, unreal.MaterialExpressionMultiply, binary(mat, unreal.MaterialExpressionMultiply, s, s, -1050, 340),
                   binary(mat, unreal.MaterialExpressionSubtract, expr(mat, C, -1150, 440, r=3.0),
                          binary(mat, unreal.MaterialExpressionMultiply, s, expr(mat, C, -1150, 500, r=2.0), -1050, 480),
                          -950, 440), -900, 360)
    cols = binary(mat, unreal.MaterialExpressionDivide, n, nu, -1450, -150)
    fu = unary(unreal.MaterialExpressionFrac, cols, -1350, -150)
    fv = unary(unreal.MaterialExpressionFrac,
               binary(mat, unreal.MaterialExpressionDivide,
                      binary(mat, unreal.MaterialExpressionAdd, unary(unreal.MaterialExpressionFloor, cols, -1350, -60),
                             eased, -1300, -20), nv, -1250, -60), -1150, -60)
    neg_v = binary(mat, unreal.MaterialExpressionMultiply, fv, expr(mat, C, -1150, 20, r=-1.0), -1050, -40)
    valid = unary(unreal.MaterialExpressionSaturate, nu_raw, -1550, 60)  # 0 when the piece has no flipbook
    flip = binary(mat, unreal.MaterialExpressionMultiply,
                  binary(mat, unreal.MaterialExpressionAppendVector, fu, neg_v, -950, -100), valid, -850, -60)
    global LAST_FLIP
    LAST_FLIP = flip  # the glow layer steps with the same flipbook (glow_layer)
    return binary(mat, unreal.MaterialExpressionAdd,
                  binary(mat, unreal.MaterialExpressionAdd, uv0, scroll, -800, -350), flip, -650, -200)


# The scenery is unlit (ASR's baked lighting in its colours). Under a CrossWorlds day course's exposure
# and lights (cfg "scenery_gain", see pipeline.ue_build) it is scaled like a surface of that colour lit by
# the course's sun and sky, so scenery and (lit) racers share one brightness scale.
SCENERY_GAIN = float(cfg.get("scenery_gain", 1.0))


def scenery_gain(mat, colour, x, y):
    k = expr(mat, unreal.MaterialExpressionConstant, x - 150, y + 100, r=SCENERY_GAIN)
    return binary(mat, unreal.MaterialExpressionMultiply, colour, k, x, y)


def emissive_scale(mat, colour, x=-100, y=150):
    """colour * EmissiveScale (default 1; ASR's light bulbs glow brighter) * SCENERY_GAIN."""
    k = expr(mat, unreal.MaterialExpressionScalarParameter, x - 150, y + 100, parameter_name="EmissiveScale",
             default_value=1.0)
    return scenery_gain(mat, binary(mat, unreal.MaterialExpressionMultiply, colour, k, x, y), x + 150, y)


def glow_layer(mat, colour):
    """ASR's self-illumination layer (a material's "_L" / "_LE" texture, texture flags 1157 / 1173; the cave's
    glowing bones, the billboard's screen frames): Glow x GlowScale added to the colour. The layer sits in
    texture slot 1 or 2 and scrolls with that slot's own UV animation ("Pan2" UV6 / "Pan3" UV7,
    export_for_unreal); GlowSlot2 = 1 picks slot 2's. It also steps with the piece's flipbook (the billboard's
    screen images and their glow change together; zero for pieces without one). Default: black (no glow)."""
    one2 = expr(mat, unreal.MaterialExpressionConstant2Vector, -2200, 2100, r=0.0, g=1.0)
    time = expr(mat, unreal.MaterialExpressionTime, -2200, 2200)
    uv0 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, 2300, coordinate_index=0)

    def scrolled(channel, y):
        rate = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, y, coordinate_index=channel)
        off = binary(mat, unreal.MaterialExpressionMultiply,
                     binary(mat, unreal.MaterialExpressionSubtract, rate, one2, -2050, y), time, -1900, y)
        return binary(mat, unreal.MaterialExpressionAdd,
                      binary(mat, unreal.MaterialExpressionAdd, uv0, off, -1750, y), LAST_FLIP, -1650, y)

    pick = expr(mat, unreal.MaterialExpressionScalarParameter, -1750, 2700, parameter_name="GlowSlot2",
                default_value=0.0)
    uv = expr(mat, unreal.MaterialExpressionLinearInterpolate, -1550, 2500)
    link(scrolled(6, 2400), "", uv, "A")
    link(scrolled(7, 2600), "", uv, "B")
    link(pick, "", uv, "Alpha")
    glow = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -1350, 2500, parameter_name="Glow",
                texture=unreal.load_asset("/Engine/EngineResources/Black"))
    link(uv, "", glow, "UVs")
    k = expr(mat, unreal.MaterialExpressionScalarParameter, -1350, 2700, parameter_name="GlowScale",
             default_value=1.0)
    return binary(mat, unreal.MaterialExpressionAdd, colour,
                  binary(mat, unreal.MaterialExpressionMultiply, glow, k, -1150, 2550, a_out="RGB"), -400, 300)


def master(name, blend, shading, two_sided, vertex_lighting=False):
    mat = new_material(name, blend, shading, two_sided)
    tex = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0, parameter_name="BaseColor")
    link(texture_uv(mat), "", tex, "UVs")
    target = EMISSIVE if shading == UNLIT else unreal.MaterialProperty.MP_BASE_COLOR
    if vertex_lighting:
        rgb, _ = vertex_colour(mat)
        colour = binary(mat, unreal.MaterialExpressionMultiply, tex, asr_lighting(mat, rgb), -200, 100, a_out="RGB")
    else:
        colour = binary(mat, unreal.MaterialExpressionMultiply, tex, expr(mat, unreal.MaterialExpressionConstant,
                                                                           -350, 150, r=1.0), -200, 100, a_out="RGB")
    if not SKINNED and shading == UNLIT:
        colour = glow_layer(mat, colour)
    mel.connect_material_property(emissive_scale(mat, colour), "", target)
    if blend == unreal.BlendMode.BLEND_MASKED:
        mel.connect_material_property(tex, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
    if shading != UNLIT:
        rough = expr(mat, unreal.MaterialExpressionConstant, -400, 300, r=0.9)
        mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    mel.recompile_material(mat)
    return mat


def translucent_master():
    """Soft-edged effects (waterfalls, light fog, cloud fades): alpha blended, texture alpha times the vertex
    alpha ASR fades their edges with, scrolling at ASR's per-piece texture rate."""
    mat = new_material("M_ASR_Translucent", unreal.BlendMode.BLEND_TRANSLUCENT, UNLIT, True)
    uv = texture_uv(mat)
    tex = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0, parameter_name="BaseColor")
    link(uv, "", tex, "UVs")
    _, alpha = vertex_colour(mat)
    mel.connect_material_property(scenery_gain(mat, binary(mat, unreal.MaterialExpressionMultiply, tex,
                                  expr(mat, unreal.MaterialExpressionConstant, -350, 150, r=1.0), -200, 100,
                                  a_out="RGB"), -50, 100), "", EMISSIVE)
    # Contrast: ASR's waterfalls show bright streaks with clear gaps; the plain texture alpha gave an even
    # veil. Opacity = saturate((alpha - AlphaFloor) * AlphaGain) * vertex alpha.
    floor = expr(mat, unreal.MaterialExpressionScalarParameter, -400, 250, parameter_name="AlphaFloor",
                 default_value=0.0)
    gain = expr(mat, unreal.MaterialExpressionScalarParameter, -400, 350, parameter_name="AlphaGain",
                default_value=1.0)
    raised = binary(mat, unreal.MaterialExpressionMultiply,
                    binary(mat, unreal.MaterialExpressionSubtract, tex, floor, -250, 250, a_out="A"), gain, -100, 250)
    sat = expr(mat, unreal.MaterialExpressionSaturate, 0, 250)
    link(raised, "", sat, "")
    mel.connect_material_property(binary(mat, unreal.MaterialExpressionMultiply, sat, alpha, 100, 250),
                                  "", unreal.MaterialProperty.MP_OPACITY)
    mel.recompile_material(mat)
    return mat


def additive_master(name="M_ASR_Additive", blend=unreal.BlendMode.BLEND_ADDITIVE):
    """Light shafts ("god rays"): additive, texture colour times its contrast-raised alpha and the vertex
    alpha, times Intensity. With blend=BLEND_TRANSLUCENT (M_ASR_Waterfall) the same layered streaks are
    alpha blended instead: colour = texture x Tint x Intensity, opacity = the streak weight. ASR's
    waterfalls are veils over the rock behind (tan with white foam veins in the reference), which
    additive blending turned into solid white sheets."""
    mat = new_material(name, blend, UNLIT, True)
    additive = blend == unreal.BlendMode.BLEND_ADDITIVE
    tex = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -400, 0, parameter_name="BaseColor")
    link(texture_uv(mat), "", tex, "UVs")
    _, alpha = vertex_colour(mat)
    streak = tex
    if not SKINNED:
        # ASR's waterfalls are layered: a second streak texture (Layer2, weight Layer2Weight) and a
        # sparkle map (Sparkle x SparkleIntensity), each scrolling at its own rate (UV6 / UV7 "Pan2" /
        # "Pan3", export_for_unreal). Streaks combine by max, so the sheet looks denser where they cross.
        one2 = expr(mat, unreal.MaterialExpressionConstant2Vector, -2200, 900, r=0.0, g=1.0)
        time = expr(mat, unreal.MaterialExpressionTime, -2200, 1000)
        uv0 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, 1100, coordinate_index=0)

        def scrolled(channel, y):
            rate = expr(mat, unreal.MaterialExpressionTextureCoordinate, -2200, y, coordinate_index=channel)
            off = binary(mat, unreal.MaterialExpressionMultiply,
                         binary(mat, unreal.MaterialExpressionSubtract, rate, one2, -2050, y), time, -1900, y)
            return binary(mat, unreal.MaterialExpressionAdd, uv0, off, -1750, y)

        tex2 = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -1500, 1200, parameter_name="Layer2")
        link(scrolled(6, 1200), "", tex2, "UVs")
        w2 = expr(mat, unreal.MaterialExpressionScalarParameter, -1500, 1400, parameter_name="Layer2Weight",
                  default_value=0.0)
        streak = binary(mat, unreal.MaterialExpressionMax, tex,
                        binary(mat, unreal.MaterialExpressionMultiply, tex2, w2, -1300, 1250, a_out="A"),
                        -1100, 1100, a_out="A")
        sparkle = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -1500, 1600, parameter_name="Sparkle")
        link(scrolled(7, 1600), "", sparkle, "UVs")
        k = expr(mat, unreal.MaterialExpressionScalarParameter, -1500, 1800, parameter_name="SparkleIntensity",
                 default_value=0.0)
        glints = binary(mat, unreal.MaterialExpressionMultiply,
                        binary(mat, unreal.MaterialExpressionMultiply, sparkle, k, -1300, 1650, a_out="RGB"),
                        alpha, -1100, 1650)
    floor = expr(mat, unreal.MaterialExpressionScalarParameter, -400, 250, parameter_name="AlphaFloor",
                 default_value=0.0)
    gain = expr(mat, unreal.MaterialExpressionScalarParameter, -400, 350, parameter_name="AlphaGain",
                default_value=1.0)
    raised = binary(mat, unreal.MaterialExpressionMultiply,
                    binary(mat, unreal.MaterialExpressionSubtract, streak, floor, -250, 250,
                           a_out="A" if streak is tex else ""), gain, -100, 250)
    sat = expr(mat, unreal.MaterialExpressionSaturate, 0, 250)
    link(raised, "", sat, "")
    weight = binary(mat, unreal.MaterialExpressionMultiply, sat, alpha, 100, 250)
    intensity = expr(mat, unreal.MaterialExpressionScalarParameter, 100, 350, parameter_name="Intensity",
                     default_value=1.0)
    if additive:
        colour = binary(mat, unreal.MaterialExpressionMultiply,
                        binary(mat, unreal.MaterialExpressionMultiply, tex, weight, 250, 100, a_out="RGB"),
                        intensity, 400, 150)
    else:
        tint = expr(mat, unreal.MaterialExpressionVectorParameter, 100, 0, parameter_name="Tint",
                    default_value=unreal.LinearColor(1, 1, 1, 1))
        tint3 = expr(mat, unreal.MaterialExpressionComponentMask, 180, 0, r=True, g=True, b=True, a=False)
        link(tint, "", tint3, "")
        colour = binary(mat, unreal.MaterialExpressionMultiply,
                        binary(mat, unreal.MaterialExpressionMultiply, tex, tint3, 250, 100, a_out="RGB"),
                        intensity, 400, 150)
        opacity = expr(mat, unreal.MaterialExpressionScalarParameter, 250, 400, parameter_name="Opacity",
                       default_value=1.0)
        mel.connect_material_property(binary(mat, unreal.MaterialExpressionMultiply, weight, opacity, 400, 350),
                                      "", unreal.MaterialProperty.MP_OPACITY)
    if not SKINNED:
        colour = binary(mat, unreal.MaterialExpressionAdd, colour, glints, 550, 200)
    mel.connect_material_property(scenery_gain(mat, colour, 700, 200), "", EMISSIVE)
    mel.recompile_material(mat)
    return mat


def water_master():
    """ASR water: in ASR (reference footage) the surface itself is invisible -- the slide under it shows at
    full colour -- and only highlights show: additive, the material's tint times the vertex alpha times a
    sparkle/highlight map (Env) moved around by a scrolling ripple normal map (Ripple, tiled like ASR's
    texture matrix)."""
    mat = new_material("M_ASR_Water", unreal.BlendMode.BLEND_ADDITIVE, UNLIT, True)
    rgb, alpha = vertex_colour(mat)
    tint = expr(mat, unreal.MaterialExpressionVectorParameter, -900, 350, parameter_name="Tint",
                default_value=unreal.LinearColor(1, 1, 1, 1))
    shine = binary(mat, unreal.MaterialExpressionMultiply, tint, alpha, -250, 500, a_out="RGB")
    tiling = expr(mat, unreal.MaterialExpressionVectorParameter, -1400, -300, parameter_name="Tiling",
                  default_value=unreal.LinearColor(1, 1, 0, 0))
    tiling2 = expr(mat, unreal.MaterialExpressionComponentMask, -1250, -300, r=True, g=True, b=False, a=False)
    link(tiling, "", tiling2, "")
    uv0 = expr(mat, unreal.MaterialExpressionTextureCoordinate, -1400, -150, coordinate_index=0)
    tiled = binary(mat, unreal.MaterialExpressionMultiply, uv0, tiling2, -1100, -200)
    ripple_uv = expr(mat, unreal.MaterialExpressionPanner, -950, -250, speed_x=0.02, speed_y=0.05)
    link(tiled, "", ripple_uv, "Coordinate")
    ripple = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -800, -250, parameter_name="Ripple")
    link(ripple_uv, "", ripple, "UVs")
    # The ripple map is a two-channel normal map (green, alpha); centre it and use it as a UV offset.
    ga = binary(mat, unreal.MaterialExpressionAppendVector, ripple, ripple, -600, -250, a_out="G", b_out="A")
    half = expr(mat, unreal.MaterialExpressionConstant, -600, -150, r=0.5)
    centred = binary(mat, unreal.MaterialExpressionSubtract, ga, half, -450, -250)
    strength = expr(mat, unreal.MaterialExpressionScalarParameter, -450, -150, parameter_name="RippleStrength",
                    default_value=0.15)
    bend = binary(mat, unreal.MaterialExpressionMultiply, centred, strength, -300, -250)
    env_uv = expr(mat, unreal.MaterialExpressionPanner, -450, -400, speed_x=-0.01, speed_y=0.02)
    link(tiled, "", env_uv, "Coordinate")
    env_uv2 = binary(mat, unreal.MaterialExpressionAdd, env_uv, bend, -250, -350)
    env = expr(mat, unreal.MaterialExpressionTextureSampleParameter2D, -100, -350, parameter_name="Env")
    link(env_uv2, "", env, "UVs")
    sparkle = expr(mat, unreal.MaterialExpressionScalarParameter, -100, -200, parameter_name="Sparkle",
                   default_value=0.6)
    glint = binary(mat, unreal.MaterialExpressionMultiply, env, sparkle, 50, -300, a_out="RGB")
    mel.connect_material_property(scenery_gain(mat, binary(mat, unreal.MaterialExpressionMultiply, glint, shine, 200, 0),
                                               350, 0), "", EMISSIVE)
    mel.recompile_material(mat)
    return mat


# The game applies its own per-stage exposure and lighting, which left lit materials black in the
# first in-game test while the unlit sky dome looked right; default to unlit (textures carry ASR's
# baked shading) until stage lighting is worked out. Set "shading": "lit" in the config to revert.
SHADING = unreal.MaterialShadingModel.MSM_DEFAULT_LIT if cfg.get("shading") == "lit" else UNLIT
# ASR's baked vertex lighting on the scenery ("vertex_lighting": true). Off: the first attempt (through
# UE's vertex-colour channel) made the course far too bright; the colours now travel in UV channels.
VERTEX_LIGHTING = bool(cfg.get("vertex_lighting", False))


def masters():
    return {"opaque": master("M_ASR_Opaque", unreal.BlendMode.BLEND_OPAQUE, SHADING, False, VERTEX_LIGHTING),
            "masked": master("M_ASR_Masked", unreal.BlendMode.BLEND_MASKED, SHADING, True, VERTEX_LIGHTING),
            "unlit": master("M_ASR_Unlit", unreal.BlendMode.BLEND_OPAQUE, UNLIT, False),  # sky dome: as authored
            "translucent": translucent_master(),
            "additive": additive_master(),
            "waterfall": additive_master("M_ASR_Waterfall", unreal.BlendMode.BLEND_TRANSLUCENT),
            "water": water_master()}


MASTERS = {False: masters()}
SKINNED = True
MASTERS[True] = masters()
SKINNED = False

# Pieces export_for_unreal found to sample soft alpha get "<material>__soft": translucent, same texture.
for name, info in list(materials.items()):
    if info.get("kind") in ("masked", "opaque") and info["texture"]:
        materials[name + "__soft"] = {**info, "kind": "translucent", "pan": 0.0}
        materials[name + "__opaque"] = {**info, "kind": "opaque", "masked": False}  # e.g. the cloud floor

SPARKLE = 1.5  # brightness of the waterfalls' sparkle specks (texture is black with white specks)


def make_instance(name, skinned):
    info = materials[name]
    kind = info.get("kind", "masked" if info["masked"] else "opaque")
    if any(s.lower() in name.lower() for s in cfg.get("unlit_match", [])):
        kind = "unlit"
    parent = MASTERS[skinned].get(kind, MASTERS[skinned]["opaque"])
    safe = re.sub(r"\W", "_", name)
    if len(safe) > 56:  # keep names unique when shortened (a material and its "__opaque" variant collided)
        safe = safe[:47] + "_" + format(zlib.crc32(safe.encode()), "08x")
    mi_name = "MI_" + safe + ("_Skin" if skinned else "")
    mi = tools.create_asset(mi_name, f"{ROOT}/Materials", unreal.MaterialInstanceConstant,
                            unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mi, parent)
    if kind == "water":
        for param, key in (("Ripple", "ripple"), ("Env", "env")):
            if info.get(key) in tex_assets:
                mel.set_material_instance_texture_parameter_value(mi, param, tex_assets[info[key]])
        r, g, b_, a_ = info.get("tint", [1, 1, 1, 1])
        mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(r, g, b_, a_))
        u, v = info.get("tiling", [1, 1])
        mel.set_material_instance_vector_parameter_value(mi, "Tiling", unreal.LinearColor(u, v, 0, 0))
    elif info["texture"] in tex_assets:
        mel.set_material_instance_texture_parameter_value(mi, "BaseColor", tex_assets[info["texture"]])
    if info.get("emissive_scale"):
        mel.set_material_instance_scalar_parameter_value(mi, "EmissiveScale", float(info["emissive_scale"]))
    if info.get("glow") in tex_assets and kind in ("opaque", "masked", "unlit") and not skinned:
        mel.set_material_instance_texture_parameter_value(mi, "Glow", tex_assets[info["glow"]])
        mel.set_material_instance_scalar_parameter_value(mi, "GlowSlot2", 1.0 if info.get("glow_slot") == 2 else 0.0)
        mel.set_material_instance_scalar_parameter_value(mi, "GlowScale", float(info.get("glow_scale", 1.0)))
    if kind == "additive" and info.get("intensity"):  # faint beams (pipeline.unreal_materials)
        mel.set_material_instance_scalar_parameter_value(mi, "Intensity", float(info["intensity"]))
    if kind in ("additive", "waterfall") and not skinned:
        if info.get("layer2") in tex_assets:
            mel.set_material_instance_texture_parameter_value(mi, "Layer2", tex_assets[info["layer2"]])
            mel.set_material_instance_scalar_parameter_value(mi, "Layer2Weight", 1.0)
        if info.get("sparkle") in tex_assets:
            mel.set_material_instance_texture_parameter_value(mi, "Sparkle", tex_assets[info["sparkle"]])
            mel.set_material_instance_scalar_parameter_value(mi, "SparkleIntensity", SPARKLE)
    if kind == "waterfall":
        # foam veins: texture alpha as is (the reference shows the rock through the gaps); the
        # normal-mapped stream crossing the slide (shd1) is cyan in the reference
        r, g, b_, a_ = info.get("tint", [1, 1, 1, 1])
        mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(r, g, b_, a_))
        mel.set_material_instance_scalar_parameter_value(mi, "AlphaGain", float(info.get("alpha_gain", 1.2)))
    return mi


instances = {norm(name): make_instance(name, False) for name in materials}
material_names = {norm(name): name for name in materials}
skin_instances = {}
log(f"created {len(instances)} material instances")

# --- Meshes -------------------------------------------------------------------
ui = unreal.FbxImportUI()
ui.set_editor_property("import_mesh", True)
ui.set_editor_property("import_as_skeletal", False)
ui.set_editor_property("import_materials", False)
ui.set_editor_property("import_textures", False)
ui.set_editor_property("import_animations", False)
ui.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH)
sm = ui.static_mesh_import_data
sm.set_editor_property("combine_meshes", False)
sm.set_editor_property("auto_generate_collision", False)
sm.set_editor_property("generate_lightmap_u_vs", False)
# The FBX stores metres but UE reads them as centimetres (checked: meshes came in 100x too
# small, axes and offsets correct), so scale up on import.
sm.set_editor_property("import_uniform_scale", 100.0)
sm.set_editor_property("convert_scene", True)
sm.set_editor_property("force_front_x_axis", False)
sm.set_editor_property("transform_vertex_to_absolute", True)
task = import_task(cfg["fbx"], f"{ROOT}/Meshes", ui)
tools.import_asset_tasks([task])
meshes = [unreal.load_asset(p) for p in task.imported_object_paths]
meshes = [m for m in meshes if isinstance(m, unreal.StaticMesh)]
missing = set()
for mesh in meshes:
    for i, slot in enumerate(mesh.static_materials):
        mi = instances.get(norm(str(slot.material_slot_name)))
        if mi is None:
            missing.add(str(slot.material_slot_name))
            continue
        mesh.set_material(i, mi)
log(f"imported {len(meshes)} static meshes; unmatched material slots: {sorted(missing)[:10]}")


# --- Animated trees (export_for_unreal: <name>_Anim_t<tree>.fbx, skinned mesh + looping animation) -----
animated = []
BREAK_OBJECTS = cfg.get("break_objects", {})
hit_sequences = {}
static_meshes = {}
for fbx in cfg.get("animated_fbx", []):
    aui = unreal.FbxImportUI()
    aui.set_editor_property("import_mesh", True)
    aui.set_editor_property("import_as_skeletal", True)
    aui.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_SKELETAL_MESH)
    aui.set_editor_property("import_materials", False)
    aui.set_editor_property("import_textures", False)
    aui.set_editor_property("import_animations", True)
    aui.set_editor_property("create_physics_asset", False)
    skd = aui.skeletal_mesh_import_data
    skd.set_editor_property("import_uniform_scale", 100.0)
    skd.set_editor_property("convert_scene", True)
    skd.set_editor_property("force_front_x_axis", False)
    skd.set_editor_property("import_morph_targets", False)
    asd = aui.anim_sequence_import_data
    asd.set_editor_property("import_uniform_scale", 100.0)
    asd.set_editor_property("convert_scene", True)
    asd.set_editor_property("force_front_x_axis", False)
    asd.set_editor_property("remove_redundant_keys", False)
    asd.set_editor_property("animation_length", unreal.FBXAnimationLengthImportType.FBXALIT_EXPORTED_TIME)
    label = os.path.splitext(os.path.basename(fbx))[0]
    dest = f"{ROOT}/Animated/{label}"
    atask = import_task(fbx, dest, aui)
    tools.import_asset_tasks([atask])
    assets = [unreal.load_asset(p) for p in atask.imported_object_paths]
    sk = next((a for a in assets if isinstance(a, unreal.SkeletalMesh)), None)
    seq = next((a for a in assets if isinstance(a, unreal.AnimSequence)), None)
    if seq is None:
        seq = next((unreal.load_asset(str(a.package_name) + "." + str(a.asset_name))
                    for a in unreal.AssetRegistryHelpers.get_asset_registry().get_assets_by_path(dest)
                    if str(a.asset_class_path.asset_name) == "AnimSequence"), None)
    if sk is None:
        log(f"{label}: no skeletal mesh imported")
        continue
    mats = sk.get_editor_property("materials")
    for i, m in enumerate(mats):
        key = norm(str(m.material_slot_name))
        if key in material_names and key not in skin_instances:
            skin_instances[key] = make_instance(material_names[key], True)
        mi = skin_instances.get(key)
        if mi is None:
            missing.add(str(m.material_slot_name))
            continue
        m.set_editor_property("material_interface", mi)
        mats[i] = m
    sk.set_editor_property("materials", mats)
    animated.append((label, sk, seq))
    if label in BREAK_OBJECTS and seq:
        # the break object's pop ("Hit", export_for_unreal) onto the same skeleton
        hui = unreal.FbxImportUI()
        hui.set_editor_property("import_mesh", False)
        hui.set_editor_property("import_as_skeletal", True)
        hui.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_ANIMATION)
        hui.set_editor_property("import_animations", True)
        hui.set_editor_property("import_materials", False)
        hui.set_editor_property("import_textures", False)
        hui.set_editor_property("skeleton", sk.get_editor_property("skeleton"))
        had = hui.anim_sequence_import_data
        had.set_editor_property("import_uniform_scale", 100.0)
        had.set_editor_property("convert_scene", True)
        had.set_editor_property("force_front_x_axis", False)
        had.set_editor_property("remove_redundant_keys", False)
        had.set_editor_property("animation_length", unreal.FBXAnimationLengthImportType.FBXALIT_EXPORTED_TIME)
        hdest = f"{dest}/Hit"
        tools.import_asset_tasks([import_task(BREAK_OBJECTS[label]["hit_fbx"], hdest, hui)])
        hit = next((unreal.load_asset(str(a.package_name) + "." + str(a.asset_name))
                    for a in unreal.AssetRegistryHelpers.get_asset_registry().get_assets_by_path(hdest)
                    if str(a.asset_class_path.asset_name) == "AnimSequence"), None)
        if hit:
            hit_sequences[label] = hit
        # its static mesh (the balloon at the bottom of its bob), shown by the break object until hit
        stask = import_task(BREAK_OBJECTS[label]["static_fbx"], f"{dest}/Static", ui)
        tools.import_asset_tasks([stask])
        for mesh in [unreal.load_asset(p) for p in stask.imported_object_paths]:
            if isinstance(mesh, unreal.StaticMesh):
                for i, slot in enumerate(mesh.static_materials):
                    mi = instances.get(norm(str(slot.material_slot_name)))
                    if mi is not None:
                        mesh.set_material(i, mi)
                static_meshes[label] = mesh
        log(f"{label}: pop animation {hit}, static mesh {static_meshes.get(label)}")
log(f"imported {len(animated)} animated trees ({sum(1 for _, _, q in animated if q)} with animation)")

# --- Level --------------------------------------------------------------------
level_path = cfg["level_path"]
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
les.new_level(level_path)
actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
for mesh in meshes:
    a = actors.spawn_actor_from_object(mesh, unreal.Vector(0, 0, 0))
    a.set_actor_label(mesh.get_name())
    is_sky = "sky" in mesh.get_name().lower()
    a.static_mesh_component.set_editor_property("cast_shadow", not is_sky)
    # The sky light captures only the sky dome: capturing the bright, saturated (unlit) scenery tinted
    # the racers' ambient light (green in game).
    a.static_mesh_component.set_editor_property("visible_in_real_time_sky_captures", is_sky)

# Lighting for the racers (the scenery is unlit with ASR's baked lighting). Replacing E-Stadium's visual
# level also removed its lights; the racers looked dark in every build with the first placeholder lights
# (sun 6, sky 1) under the game's exposure, so these are stronger. Only lit objects use them.
LIGHT = cfg.get("lighting", {})
for label, sk, seq in animated:
    if label in hit_sequences:
        continue  # a break object (below)
    a = actors.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(0, 0, 0))
    a.set_actor_label(label)
    comp = a.skeletal_mesh_component
    comp.set_skeletal_mesh_asset(sk) if hasattr(comp, "set_skeletal_mesh_asset") else comp.set_skeletal_mesh(sk)
    comp.set_editor_property("visible_in_real_time_sky_captures", False)
    # Always animate (the scenery is seen from far away) and keep bounds large enough for the motion.
    comp.set_editor_property("visibility_based_anim_tick_option",
                             unreal.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
    comp.set_editor_property("bounds_scale", 4.0)
    if seq:
        comp.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_SINGLE_NODE)
        play = unreal.SingleAnimationPlayData()
        play.set_editor_property("anim_to_play", seq)
        play.set_editor_property("saved_looping", True)
        play.set_editor_property("saved_playing", True)
        play.set_editor_property("saved_play_rate", 1.0)
        # ASR staggers placed objects' loops (locator SequenceStartFrameMultiplier: 0, 1/3, 2/3 of the
        # loop) -- the three balloons of a row bob at different times instead of colliding
        start = float(cfg.get("anim_start", {}).get("Anim_" + label.split("_Anim_")[-1], 0.0))
        if start:
            play.set_editor_property("saved_position", start * seq.get_editor_property("sequence_length"))
        comp.set_editor_property("animation_data", play)

# ASR's balloons pop when a racer hits them: CrossWorlds' native break object (ABreakObjectBase, the class
# behind the stock breakable rocks). In game it shows the static mesh (here the balloon at the bottom of its
# bob) until hit, then hides it (and its collision) -- a skeletal mesh alone stayed invisible -- and leaves
# the break visuals to its Blueprint's OnPlayBreakAnimation event (the stock rock's Blueprint implements it).
# BP_ASR_BalloonBreak (generated by the AsrEditorTools plugin, tools/ue_plugin) shows the skeletal mesh there
# and plays it from the start: ASR's pop ("Hit": balloon gone at frame 4, burst sprite, flash).
# Its collision body sits in domain 0 (the main course): the default, "invalid", never collided (the first
# try, a VAT break object, did nothing in game). The balloons are lowered so the bottom of their bob rests
# BREAK["lift"] cm above our road (their bob starts ~7 m up, out of reach); the box covers the balloon there.
BREAK = cfg.get("break_object", {})
break_class = None
if hit_sequences:
    bp = unreal.AsrAnimTools.make_play_on_event_blueprint(f"{ROOT}/Blueprints", "BP_ASR_BalloonBreak",
                                                         unreal.BreakObjectBase.static_class(),
                                                         "OnPlayBreakAnimation", "SkeletalMeshComponent")
    if bp is None:
        log("BP_ASR_BalloonBreak FAILED")
    else:
        eal.save_loaded_asset(bp)
        break_class = bp.generated_class()


def break_centre(label):
    lo, hi = BREAK_OBJECTS[label]["bounds"]["low_min"], BREAK_OBJECTS[label]["bounds"]["low_max"]
    return ((lo[0] + hi[0]) * 50, -(lo[1] + hi[1]) * 50)  # Unreal x, y (cm)


# The balloons' bob phase: ASR's rows of three alternate -- the first row's outer balloons low while its middle
# one is high, the second row the other way round (the outer two of a row always level). Rows are the balloons
# within 30 m of each other, in lap order (ASR's locator numbers run along the lap); the bob starts at its
# bottom, so "high" = half a loop in.
break_phase = {}
break_labels = sorted((l for l, _, _ in animated if l in hit_sequences), key=lambda l: int(l.rsplit("_i", 1)[-1]))
rows = []
for l in break_labels:
    row = next((r for r in rows if math.dist(break_centre(r[0]), break_centre(l)) < 3000), None)
    row.append(l) if row else rows.append([l])
for k, row in enumerate(rows):
    mid = [sum(break_centre(l)[i] for l in row) / len(row) for i in (0, 1)]
    middle = min(row, key=lambda l: math.dist(break_centre(l), mid))
    for l in row:
        high = (l == middle) == (k % 2 == 0)
        break_phase[l] = 0.5 if high else 0.0
log(f"balloon rows: {[[(l.split('_Anim_')[-1], break_phase[l]) for l in r] for r in rows]}")
# Each row hangs level, as in ASR: its balloons' low point at the same height, BREAK["lift"] above the highest
# road under the row (lowered one by one, a row on a slope came out uneven in game).
row_bottom = {}
for row in rows:
    grounds = [cfg.get("break_ground", {}).get(l.rsplit("_i", 1)[-1]) for l in row]
    if all(g is not None for g in grounds):
        for l in row:
            row_bottom[l] = max(grounds) + float(BREAK.get("lift", 30.0))

for label, sk, seq in animated:
    if label not in hit_sequences or break_class is None:
        continue
    # the balloon at the bottom of its bob (it floats ~10 m up and down): the collision body can't follow
    # the animation, so it covers the balloon where a kart can reach it
    lo, hi = BREAK_OBJECTS[label]["bounds"]["low_min"], BREAK_OBJECTS[label]["bounds"]["low_max"]
    # Blender (x, y, z) m -> Unreal (x, -y, z) cm (FBX export + import)
    lo_ue = [lo[0] * 100, -hi[1] * 100, lo[2] * 100]
    hi_ue = [hi[0] * 100, -lo[1] * 100, hi[2] * 100]
    c = [(l + h) / 2 for l, h in zip(lo_ue, hi_ue)]
    e = [(h - l) / 2 for l, h in zip(lo_ue, hi_ue)]
    ground = cfg.get("break_ground", {}).get(label.rsplit("_i", 1)[-1])
    shift = ground + float(BREAK.get("lift", 30.0)) - lo_ue[2] if ground is not None else 0.0
    if label in row_bottom:
        shift = row_bottom[label] - lo_ue[2]
    a = actors.spawn_actor_from_class(break_class, unreal.Vector(c[0], c[1], c[2] + shift))
    a.set_actor_label(label)
    comp = a.get_editor_property("skeletal_mesh_component")
    comp.set_skeletal_mesh_asset(sk) if hasattr(comp, "set_skeletal_mesh_asset") else comp.set_skeletal_mesh(sk)
    comp.set_editor_property("relative_location", unreal.Vector(-c[0], -c[1], -c[2]))  # vertices are absolute
    comp.set_editor_property("visible_in_real_time_sky_captures", False)
    comp.set_editor_property("visibility_based_anim_tick_option",
                             unreal.VisibilityBasedAnimTickOption.ALWAYS_TICK_POSE_AND_REFRESH_BONES)
    comp.set_editor_property("bounds_scale", 4.0)
    comp.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_SINGLE_NODE)
    # ASR's idle bob, looping (phase: break_phase); the mod (CustomGrandPrix) shows this
    # skeletal mesh instead of the static one, switches it to the pop (<label>/Hit/<label>_Hit_Anim) on a hit
    # and back to this loop when the balloon returns. Without the mod the static balloon shows, as before.
    play = unreal.SingleAnimationPlayData()
    play.set_editor_property("anim_to_play", seq or hit_sequences[label])
    play.set_editor_property("saved_looping", bool(seq))
    play.set_editor_property("saved_playing", bool(seq))
    start = break_phase.get(label, 0.0)
    if seq and start:
        play.set_editor_property("saved_position", start * seq.get_editor_property("sequence_length"))
    comp.set_editor_property("animation_data", play)
    if seq:
        # The cook packs only what the level references: the pop animation (played by the mod) is kept by a
        # hidden actor holding it (with the balloon idle in the break object, the pop went missing in game).
        holder = actors.spawn_actor_from_class(unreal.SkeletalMeshActor, unreal.Vector(c[0], c[1], c[2] + shift))
        holder.set_actor_label(f"{label}_PopHolder")
        hc = holder.skeletal_mesh_component
        hc.set_skeletal_mesh_asset(sk) if hasattr(hc, "set_skeletal_mesh_asset") else hc.set_skeletal_mesh(sk)
        hc.set_editor_property("animation_mode", unreal.AnimationMode.ANIMATION_SINGLE_NODE)
        hold = unreal.SingleAnimationPlayData()
        hold.set_editor_property("anim_to_play", hit_sequences[label])
        hold.set_editor_property("saved_playing", False)
        hc.set_editor_property("animation_data", hold)
        hc.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        hc.set_editor_property("hidden_in_game", True)
        hc.set_editor_property("visible", False)
        holder.set_actor_hidden_in_game(True)
    if label in static_meshes:
        smc = a.get_editor_property("static_mesh_component")
        smc.set_static_mesh(static_meshes[label])
        smc.set_editor_property("relative_location", unreal.Vector(-c[0], -c[1], -c[2]))
        smc.set_editor_property("visible_in_real_time_sky_captures", False)
    body = a.get_editor_property("static_body_component")
    body.set_editor_property("body_type", unreal.StaticBodyShapeType.STATIC_BODY_BOX)
    box = unreal.StaticBodyBoxParam()
    # a little larger than the balloon: it pops when the kart touches it, not once inside
    k = float(BREAK.get("body_scale", 1.1))
    box.set_editor_property("half_extents", unreal.Vector(e[0] * k, e[1] * k, e[2]))
    body.set_editor_property("box_param", box)
    body.set_editor_property("domain_number", unreal.DomainNumber.DOMAIN_NUMBER0)
    for prop, value in (("break_condition_type", unreal.BreakConditionType.ALL),
                        ("break_damage_type", unreal.BreakDamageType.NONE),
                        ("is_break", True),
                        ("is_relocate", True),
                        # back after 2 s (5 s felt far too long; ASR's own time lives in its code)
                        ("relocate_timer", float(BREAK.get("relocate_seconds", 2.0)))):
        a.set_editor_property(prop, value)
    origin, extent = a.get_actor_bounds(False)
    log(f"break object {label}: centre {[round(v) for v in c]} half {[round(v) for v in e]} lowered {shift:.0f} cm, "
        f"actor bounds {origin} +- {extent}")

pitch, yaw = LIGHT.get("sun_rotation", [-50.0, 30.0])
sun = actors.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000),
                                    unreal.Rotator(roll=0.0, pitch=float(pitch), yaw=float(yaw)))
sun.light_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
sun.light_component.set_editor_property("intensity", float(LIGHT.get("sun", 12.0)))
if LIGHT.get("sun_color"):
    sun.light_component.set_editor_property("light_color", unreal.Color(*[int(round(c * 255)) for c in LIGHT["sun_color"]], 255))
# The course's ambient ("sky") light. A sky light ignores lighting channels and a movable one lights
# every loaded level -- it brightened the travel-ring course loaded alongside ours. Stock courses use
# static sky lights baked into their own scenery. So the sky light is emulated with shadowless
# directional fills on lighting channel 0 (the main course's, like the stock suns; the travel courses
# use channels 1-2): "sky_fill" = [{az_from_sun, elevation, rgb}], fitted by pipeline.sky_fill to the
# irradiance of a CrossWorlds sky HDRI at Metal Harbor's sky-light intensity.
for fill in LIGHT.get("sky_fill", []):
    rgb = [float(c) for c in fill["rgb"]]
    peak = max(rgb)
    if peak <= 0:
        continue
    f = actors.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 50000),
                                      unreal.Rotator(roll=0.0, pitch=-float(fill["elevation"]),
                                                     yaw=float(yaw) + float(fill["az_from_sun"])))
    f.set_actor_label(f"SkyFill_{fill['name']}")
    lc = f.light_component
    lc.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    lc.set_editor_property("intensity", peak)
    lc.set_editor_property("light_color", unreal.Color(*[int(round(c / peak * 255)) for c in rgb], 255))
    lc.set_editor_property("cast_shadows", False)
    lc.set_editor_property("specular_scale", 0.0)
    lc.set_editor_property("atmosphere_sun_light", False)
log(f"sky fill lights: {len(LIGHT.get('sky_fill', []))}")

# Post-process overrides ("lighting": {"post_process": {...}}) go in a volume bounded to our scenery:
# an unbound one also reached the travel-ring course (its level is loaded alongside ours). The values
# are a stock day course's own volumes (Metal Harbor, runtime dump), so exposure works as in the game.
PP = LIGHT.get("post_process") or {}
if PP:
    lo = hi = None
    for a in actors.get_all_level_actors():
        if isinstance(a, unreal.StaticMeshActor) and "sky" not in a.get_actor_label().lower():
            origin, extent = a.get_actor_bounds(False)
            amin, amax = origin - extent, origin + extent
            lo = amin if lo is None else unreal.Vector(min(lo.x, amin.x), min(lo.y, amin.y), min(lo.z, amin.z))
            hi = amax if hi is None else unreal.Vector(max(hi.x, amax.x), max(hi.y, amax.y), max(hi.z, amax.z))
    # 5 km around the scenery: the stage-intro camera flies ~1.5 km up (outside a tight volume the game's
    # default auto exposure applied on top of the scenery gain: washed out). Other stages' level
    # instances sit 18 km away.
    margin = 500000.0
    centre = (lo + hi) * 0.5
    half = (hi - lo) * 0.5 + unreal.Vector(margin, margin, margin)
    ppv = actors.spawn_actor_from_class(unreal.PostProcessVolume, centre)
    ppv.set_actor_label("PPV_ASR")
    ppv.set_actor_scale3d(half * 0.01)  # the default volume brush is 200 units across
    # "post_process_unbound": bounded volumes were not applied in game (see pipeline.ue_build)
    ppv.set_editor_property("unbound", bool(LIGHT.get("post_process_unbound", False)))
    ppv.set_editor_property("priority", float(LIGHT.get("post_process_priority", 100.0)))
    ppv.set_editor_property("blend_radius", 0.0)
    pp = ppv.get_editor_property("settings")
    enums = {"auto_exposure_method": unreal.AutoExposureMethod, "bloom_method": unreal.BloomMethod}
    for prop, value in PP.items():
        if prop in enums and isinstance(value, str):
            value = getattr(enums[prop], value)
        elif isinstance(value, list) and len(value) == 4:
            value = unreal.Vector4(*value)
        pp.set_editor_property("override_" + prop, True)
        pp.set_editor_property(prop, value)
    ppv.set_editor_property("settings", pp)
    log(f"post-process volume over {centre} +- {half}: {PP}")
log(f"lighting: sun {sun.light_component.get_editor_property('intensity')}")

les.save_current_level()
eal.save_directory(ROOT, only_if_is_dirty=False, recursive=True)
log(f"saved level {level_path} with {len(meshes)} mesh actors")
log("BUILD_OK")
unreal.SystemLibrary.quit_editor()
