"""Unreal Editor Python: create empty levels at the given paths (diagnostic: blank a stage's sublevels).

    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript=tools/ue_blank_levels.py -nullrhi
    (UE_BLANK_LEVELS env var -> comma-separated /Game/... level paths; delete their files on disk first)
"""

import os

import unreal

les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
for path in os.environ["UE_BLANK_LEVELS"].split(","):
    ok = les.new_level(path) and les.save_current_level()
    unreal.log(f"[BLANK] {path}: {'saved' if ok else 'FAILED'}")
unreal.log("[BLANK] BLANK_OK")
unreal.SystemLibrary.quit_editor()
