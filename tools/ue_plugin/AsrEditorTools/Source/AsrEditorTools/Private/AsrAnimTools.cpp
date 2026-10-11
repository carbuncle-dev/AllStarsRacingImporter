#include "AsrAnimTools.h"

#include "AnimGraphNode_BlendListByBool.h"
#include "AnimGraphNode_Root.h"
#include "AnimGraphNode_SequencePlayer.h"
#include "AnimNodes/AnimNode_BlendListByBool.h"
#include "Animation/AnimBlueprint.h"
#include "Animation/AnimSequence.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetToolsModule.h"
#include "EdGraph/EdGraph.h"
#include "EdGraphSchema_K2.h"
#include "Factories/AnimBlueprintFactory.h"
#include "IAssetTools.h"
#include "K2Node_VariableGet.h"
#include "Components/SkeletalMeshComponent.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"
#include "K2Node_CallFunction.h"
#include "K2Node_Event.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "UObject/Package.h"

DEFINE_LOG_CATEGORY_STATIC(LogAsrTools, Display, All);

namespace
{
	// Node settings are private/protected (and folded) in the runtime structs: set through reflection.
	void SetBool(UScriptStruct* Struct, void* Data, const TCHAR* Name, bool Value)
	{
		if (FBoolProperty* Prop = CastField<FBoolProperty>(Struct->FindPropertyByName(Name)))
			Prop->SetPropertyValue_InContainer(Data, Value);
		else
			UE_LOG(LogAsrTools, Warning, TEXT("[ASRTOOLS] %s has no bool %s"), *Struct->GetName(), Name);
	}

	void SetFloat(UScriptStruct* Struct, void* Data, const TCHAR* Name, float Value)
	{
		if (FFloatProperty* Prop = CastField<FFloatProperty>(Struct->FindPropertyByName(Name)))
			Prop->SetPropertyValue_InContainer(Data, Value);
		else
			UE_LOG(LogAsrTools, Warning, TEXT("[ASRTOOLS] %s has no float %s"), *Struct->GetName(), Name);
	}

	UEdGraphPin* FirstPin(UEdGraphNode* Node, EEdGraphPinDirection Direction, int32 Index = 0)
	{
		int32 Seen = 0;
		for (UEdGraphPin* Pin : Node->Pins)
			if (Pin->Direction == Direction && Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Struct && Seen++ == Index)
				return Pin;
		return nullptr;
	}

	bool Connect(UEdGraph* Graph, UEdGraphPin* A, UEdGraphPin* B)
	{
		const bool Ok = A && B && Graph->GetSchema()->TryCreateConnection(A, B);
		UE_LOG(LogAsrTools, Display, TEXT("[ASRTOOLS] connect %s -> %s: %s"), A ? *A->PinName.ToString() : TEXT("?"),
		       B ? *B->PinName.ToString() : TEXT("?"), Ok ? TEXT("ok") : TEXT("FAILED"));
		return Ok;
	}
}

UAnimBlueprint* UAsrAnimTools::MakeToggleAnimBlueprint(const FString& PackagePath, const FString& Name, UClass* ParentClass,
                                                       USkeleton* Skeleton, UAnimSequence* Idle, UAnimSequence* Hit,
                                                       FName BoolName, float IdleStart)
{
	UAnimBlueprintFactory* Factory = NewObject<UAnimBlueprintFactory>();
	Factory->BlueprintType = BPTYPE_Normal;
	Factory->ParentClass = ParentClass;
	Factory->TargetSkeleton = Skeleton;
	IAssetTools& Tools = FModuleManager::LoadModuleChecked<FAssetToolsModule>("AssetTools").Get();
	UAnimBlueprint* Blueprint = Cast<UAnimBlueprint>(Tools.CreateAsset(Name, PackagePath, UAnimBlueprint::StaticClass(), Factory));
	if (!Blueprint)
	{
		UE_LOG(LogAsrTools, Error, TEXT("[ASRTOOLS] could not create %s/%s"), *PackagePath, *Name);
		return nullptr;
	}
	UEdGraph* Graph = nullptr;
	for (UEdGraph* G : Blueprint->FunctionGraphs)
		if (G->GetFName() == UEdGraphSchema_K2::GN_AnimGraph)
			Graph = G;
	UAnimGraphNode_Root* Root = nullptr;
	if (Graph)
		for (UEdGraphNode* Node : Graph->Nodes)
			if (UAnimGraphNode_Root* R = Cast<UAnimGraphNode_Root>(Node))
				Root = R;
	if (!Graph || !Root)
	{
		UE_LOG(LogAsrTools, Error, TEXT("[ASRTOOLS] %s: no AnimGraph / root node"), *Name);
		return nullptr;
	}

	auto MakePlayer = [&](UAnimSequence* Sequence, bool bLoop, float Start, int32 Y)
	{
		FGraphNodeCreator<UAnimGraphNode_SequencePlayer> Creator(*Graph);
		UAnimGraphNode_SequencePlayer* Node = Creator.CreateNode();
		Node->NodePosX = -700;
		Node->NodePosY = Y;
		Creator.Finalize();
		Node->SetAnimationAsset(Sequence);
		UScriptStruct* Struct = FAnimNode_SequencePlayer::StaticStruct();
		SetBool(Struct, &Node->Node, TEXT("bLoopAnimation"), bLoop);
		SetFloat(Struct, &Node->Node, TEXT("StartPosition"), Start);
		return Node;
	};
	UAnimGraphNode_SequencePlayer* HitPlayer = MakePlayer(Hit, false, 0.0f, 0);
	UAnimGraphNode_SequencePlayer* IdlePlayer = MakePlayer(Idle, true, IdleStart, 250);

	FGraphNodeCreator<UAnimGraphNode_BlendListByBool> BlendCreator(*Graph);
	UAnimGraphNode_BlendListByBool* Blend = BlendCreator.CreateNode();
	Blend->NodePosX = -300;
	BlendCreator.Finalize();
	{
		UScriptStruct* Struct = FAnimNode_BlendListByBool::StaticStruct();
		if (FProperty* Prop = Struct->FindPropertyByName(TEXT("BlendTime")))
			for (float& Time : *Prop->ContainerPtrToValuePtr<TArray<float>>(&Blend->Node))
				Time = 0.0f;
		SetBool(Struct, &Blend->Node, TEXT("bResetChildOnActivation"), true);
	}

	FGraphNodeCreator<UK2Node_VariableGet> GetCreator(*Graph);
	UK2Node_VariableGet* Get = GetCreator.CreateNode();
	Get->VariableReference.SetSelfMember(BoolName);
	Get->NodePosX = -700;
	Get->NodePosY = -150;
	GetCreator.Finalize();

	for (UEdGraphNode* Node : TArray<UEdGraphNode*>{Blend, Get, Root, HitPlayer})
		for (UEdGraphPin* Pin : Node->Pins)
			UE_LOG(LogAsrTools, Display, TEXT("[ASRTOOLS] %s pin %s (%s, %s)"), *Node->GetClass()->GetName(),
			       *Pin->PinName.ToString(), Pin->Direction == EGPD_Input ? TEXT("in") : TEXT("out"),
			       *Pin->PinType.PinCategory.ToString());

	// BlendListByBool: pose 0 is used while the bool is true, pose 1 while it is false
	UEdGraphPin* GetOut = nullptr;
	for (UEdGraphPin* Pin : Get->Pins)
		if (Pin->Direction == EGPD_Output)
			GetOut = Pin;
	bool Ok = true;
	Ok &= Connect(Graph, FirstPin(HitPlayer, EGPD_Output), FirstPin(Blend, EGPD_Input, 0));
	Ok &= Connect(Graph, FirstPin(IdlePlayer, EGPD_Output), FirstPin(Blend, EGPD_Input, 1));
	Ok &= Connect(Graph, GetOut, Blend->FindPin(TEXT("bActiveValue"), EGPD_Input));
	Ok &= Connect(Graph, FirstPin(Blend, EGPD_Output), FirstPin(Root, EGPD_Input));

	FKismetEditorUtilities::CompileBlueprint(Blueprint);
	UE_LOG(LogAsrTools, Display, TEXT("[ASRTOOLS] %s: graph %s, compile status %d"), *Name, Ok ? TEXT("ok") : TEXT("INCOMPLETE"),
	       (int32)Blueprint->Status);
	return Ok ? Blueprint : nullptr;
}

UBlueprint* UAsrAnimTools::MakePlayOnEventBlueprint(const FString& PackagePath, const FString& Name, UClass* ParentClass,
                                                   FName EventName, FName ComponentName)
{
	UPackage* Package = CreatePackage(*(PackagePath / Name));
	UBlueprint* Blueprint = FKismetEditorUtilities::CreateBlueprint(ParentClass, Package, FName(*Name), BPTYPE_Normal,
	                                                                UBlueprint::StaticClass(), UBlueprintGeneratedClass::StaticClass());
	if (!Blueprint)
	{
		UE_LOG(LogAsrTools, Error, TEXT("[ASRTOOLS] could not create %s/%s"), *PackagePath, *Name);
		return nullptr;
	}
	FAssetRegistryModule::AssetCreated(Blueprint);
	UEdGraph* Graph = FBlueprintEditorUtils::FindEventGraph(Blueprint);
	int32 Y = 0;
	UK2Node_Event* Event = FKismetEditorUtilities::AddDefaultEventNode(Blueprint, Graph, EventName, ParentClass, Y);
	if (!Event)
	{
		UE_LOG(LogAsrTools, Error, TEXT("[ASRTOOLS] %s: no event %s"), *Name, *EventName.ToString());
		return nullptr;
	}
	const UEdGraphSchema_K2* Schema = GetDefault<UEdGraphSchema_K2>();

	FGraphNodeCreator<UK2Node_VariableGet> GetCreator(*Graph);
	UK2Node_VariableGet* Get = GetCreator.CreateNode();
	Get->VariableReference.SetSelfMember(ComponentName);
	Get->NodePosX = Event->NodePosX + 100;
	Get->NodePosY = Event->NodePosY + 200;
	GetCreator.Finalize();
	UEdGraphPin* Component = nullptr;
	for (UEdGraphPin* Pin : Get->Pins)
		if (Pin->Direction == EGPD_Output)
			Component = Pin;

	bool Ok = Component != nullptr;
	UEdGraphPin* Exec = Event->FindPin(UEdGraphSchema_K2::PN_Then);
	int32 X = Event->NodePosX + 350;
	auto Call = [&](UClass* Class, const TCHAR* Function, TArray<TPair<FString, FString>> Defaults)
	{
		FGraphNodeCreator<UK2Node_CallFunction> Creator(*Graph);
		UK2Node_CallFunction* Node = Creator.CreateNode();
		Node->SetFromFunction(Class->FindFunctionByName(Function));
		Node->NodePosX = X;
		Node->NodePosY = Event->NodePosY;
		X += 300;
		Creator.Finalize();
		for (const TPair<FString, FString>& Default : Defaults)
		{
			if (UEdGraphPin* Pin = Node->FindPin(*Default.Key))
				Schema->TrySetDefaultValue(*Pin, Default.Value);
			else
			{
				UE_LOG(LogAsrTools, Warning, TEXT("[ASRTOOLS] %s has no pin %s"), Function, *Default.Key);
				Ok = false;
			}
		}
		Ok &= Connect(Graph, Exec, Node->GetExecPin());
		Ok &= Connect(Graph, Component, Node->FindPin(UEdGraphSchema_K2::PN_Self));
		Exec = Node->GetThenPin();
	};
	Call(USceneComponent::StaticClass(), TEXT("SetVisibility"), {{TEXT("bNewVisibility"), TEXT("true")}, {TEXT("bPropagateToChildren"), TEXT("true")}});
	Call(USceneComponent::StaticClass(), TEXT("SetHiddenInGame"), {{TEXT("NewHidden"), TEXT("false")}, {TEXT("bPropagateToChildren"), TEXT("true")}});
	Call(USkeletalMeshComponent::StaticClass(), TEXT("SetPosition"), {{TEXT("InPos"), TEXT("0.0")}, {TEXT("bFireNotifies"), TEXT("false")}});
	Call(USkeletalMeshComponent::StaticClass(), TEXT("Play"), {{TEXT("bLooping"), TEXT("false")}});

	FKismetEditorUtilities::CompileBlueprint(Blueprint);
	UE_LOG(LogAsrTools, Display, TEXT("[ASRTOOLS] %s: event graph %s, compile status %d"), *Name, Ok ? TEXT("ok") : TEXT("INCOMPLETE"),
	       (int32)Blueprint->Status);
	return Ok ? Blueprint : nullptr;
}
