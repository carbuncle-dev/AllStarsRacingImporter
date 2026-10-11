using UnrealBuildTool;

public class AsrEditorTools : ModuleRules
{
	public AsrEditorTools(ReadOnlyTargetRules Target) : base(Target)
	{
		// Own compile (no engine shared PCH): UE 5.4's headers fail under MSVC 14.42 on __has_feature
		// (ConcurrentLinearAllocator.h); the define keeps them compiling. Built alone with -Module.
		PCHUsage = PCHUsageMode.NoPCHs;
		bUseUnity = false;
		PrivateDefinitions.Add("__has_feature(x)=0");
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine" });
		PrivateDependencyModuleNames.AddRange(new string[] {
			"UnrealEd", "AnimGraph", "AnimGraphRuntime", "BlueprintGraph", "AssetTools", "Kismet", "AssetRegistry" });
	}
}
