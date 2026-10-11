#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "AsrAnimTools.generated.h"

class UAnimBlueprint;
class UAnimSequence;
class USkeleton;

// Anim Blueprint authoring for tools/ue_build_level.py (editor Python cannot create anim graph nodes).
UCLASS()
class UAsrAnimTools : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	// Creates <PackagePath>/<Name>: an Anim Blueprint deriving from ParentClass whose AnimGraph plays Idle
	// (looping, starting IdleStart seconds in) while the bool member BoolName is false and Hit once (from the
	// start, again on every activation) while it is true, switching without blending. Compiled, not saved.
	UFUNCTION(BlueprintCallable, Category = "ASR")
	static UAnimBlueprint* MakeToggleAnimBlueprint(const FString& PackagePath, const FString& Name, UClass* ParentClass,
	                                               USkeleton* Skeleton, UAnimSequence* Idle, UAnimSequence* Hit,
	                                               FName BoolName, float IdleStart);

	// Creates <PackagePath>/<Name>: a Blueprint deriving from ParentClass that implements the event EventName
	// as: <ComponentName>.SetVisibility(true), .SetHiddenInGame(false), .SetPosition(0), .Play(false)
	// (ComponentName: a SkeletalMeshComponent member in single-node mode). Compiled, not saved.
	UFUNCTION(BlueprintCallable, Category = "ASR")
	static UBlueprint* MakePlayOnEventBlueprint(const FString& PackagePath, const FString& Name, UClass* ParentClass,
	                                            FName EventName, FName ComponentName);
};
