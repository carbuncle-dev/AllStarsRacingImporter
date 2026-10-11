"""Give a custom stage its own race music: a new BGM selector label next to the template stage's.

    build(label="stg3001_RockyCoaster", template="stg1016_EStadium", sheet="BGM_ASR3001") -> files

Race music in CrossWorlds: the stage's data asset names a label (BGMName, e.g. "stg1016_EStadium"); the race
plays the shared cue sheet BGM.acb's race cues (BGM_INTRO, BGM_LAP1/2/3, transitions...), which are switch
sequences on the selectors BGM_INTRO / BGM_LAP1 / ... with one branch per label. A stage's branch plays a
sequence whose action track starts a cue in the stage's own sheet, named by string (TargetAcbName "BGM_STG1016",
TargetId = cue id). Labels are defined in the project settings (ACF, embedded in the SoundAtomConfig asset
/Game/01_Union/Sound/Config/UNION): per selector a contiguous range of the ACF's StringValueTable
(LabelStartIndex, NumLabels; GlobalLabelIndex is relative to the range). Game updates add DLC labels at the end
of these ranges.

So the new label is added
  * to the ACF: at the end of each selector's range that has the template label (later ranges shift by one);
  * to BGM.acb: every switch track for the template label is copied (selector command -> the new label), with a
    copy of the sequence it plays whose action tracks target the new sheet (same cue ids);
  * the new sheet is the track's BGM file under its own name (pipeline.music --file-stage: the ACB's own Name
    field renamed here, so the sound engine registers it separately from the stock sheet).
Stock labels, branches and sheets are unchanged. Only rebuilt tables change; the rest is copied as is.
"""

from __future__ import annotations

import struct
from pathlib import Path

from cw.utf import UtfTable, build_utf, parse_utf
from pipeline.custom_stage import resized

ROOT = Path(__file__).resolve().parents[1]
BGM_ACB = ROOT / "extracted" / "cw" / "criware_bgm" / "UNION" / "Content" / "CriWareData" / "BGM.acb"
CONFIG = ROOT / "extracted" / "cw" / "sound_config" / "UNION" / "Content" / "01_Union" / "Sound" / "Config" / "UNION"
ACF_AT = 14  # the ACF starts after the export's property header


def commands(blob: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i + 3 <= len(blob):
        code, n = struct.unpack_from(">HB", blob, i)
        out.append((code, blob[i + 3:i + 3 + n]))
        i += 3 + n
    return out


def encode(cmds: list[tuple[int, bytes]]) -> bytes:
    return b"".join(struct.pack(">HB", c, len(p)) + p for c, p in cmds)


def u16s(blob: bytes) -> list[int]:
    return [struct.unpack_from(">H", blob, i)[0] for i in range(0, len(blob), 2)]


def add_acf_label(config_uexp: bytes, selectors: set[str], template: str, label: str) -> bytes:
    size = struct.unpack_from(">I", config_uexp, ACF_AT + 4)[0] + 8
    acf = config_uexp[ACF_AT:ACF_AT + size]
    top = parse_utf(acf)
    strings = parse_utf(top.rows[0]["StringValueTable"])
    sels = parse_utf(top.rows[0]["SelectorTable"])
    values = [r["StringValue"] for r in strings.rows]
    inserts = []
    for s in sels.rows:
        start, n = s["LabelStartIndex"], s["NumLabels"]
        names = values[start:start + n]
        if s["Name"] in selectors:
            assert template in names, (s["Name"], template)
            assert label not in names
            inserts.append(start + n)
    for at in sorted(inserts, reverse=True):
        strings.rows.insert(at, {"StringValue": label})
        for s in sels.rows:
            if s["LabelStartIndex"] >= at and not (s["LabelStartIndex"] + s["NumLabels"] == at):
                s["LabelStartIndex"] += 1
            if s["LabelStartIndex"] + s["NumLabels"] == at and s["Name"] in selectors:
                pass
        for s in sels.rows:  # the selector whose range ends here grows
            if s["Name"] in selectors and s["LabelStartIndex"] + s["NumLabels"] == at:
                s["NumLabels"] += 1
    # check: every selector still lists the same labels, the edited ones plus the new label at the end
    new_values = [r["StringValue"] for r in strings.rows]
    old = {s["Name"]: s for s in parse_utf(top.rows[0]["SelectorTable"]).rows}
    for s in sels.rows:
        o = old[s["Name"]]
        before = values[o["LabelStartIndex"]:o["LabelStartIndex"] + o["NumLabels"]]
        after = new_values[s["LabelStartIndex"]:s["LabelStartIndex"] + s["NumLabels"]]
        assert after == (before + [label] if s["Name"] in selectors else before), s["Name"]
    top.rows[0]["StringValueTable"] = build_utf(strings)
    top.rows[0]["SelectorTable"] = build_utf(sels)
    new_acf = build_utf(top, 32)
    return config_uexp[:ACF_AT] + new_acf + config_uexp[ACF_AT + size:]


def add_acb_branches(acb: bytes, template: str, label: str, stock_sheet: str, sheet: str) -> tuple[bytes, set[str]]:
    top = parse_utf(acb)
    h = top.rows[0]
    T = {n: parse_utf(h[n]) for n in ("StringValueTable", "TrackTable", "TrackCommandTable", "TrackEventTable",
                                      "SequenceTable", "ActionTrackTable")}
    sv = [r["StringValue"] for r in T["StringValueTable"].rows]
    template_i = sv.index(template)
    assert label not in sv
    T["StringValueTable"].rows.append({"StringValue": label})
    label_i = len(sv)
    tracks, tcmd, tev = T["TrackTable"].rows, T["TrackCommandTable"].rows, T["TrackEventTable"].rows
    seqs, actions = T["SequenceTable"].rows, T["ActionTrackTable"].rows
    selectors, cloned = set(), {}
    for s_i, s in enumerate(list(seqs)):
        if not s["TrackIndex"]:
            continue
        new_tracks = []
        for t_i in u16s(s["TrackIndex"]):
            t = tracks[t_i]
            if t["CommandIndex"] == 65535:
                continue
            cmds = commands(tcmd[t["CommandIndex"]]["Command"])
            sel = [p for c, p in cmds if c == 100 and len(p) == 4 and struct.unpack(">H", p[2:])[0] == template_i]
            if not sel:
                continue
            selectors.add(sv[struct.unpack(">H", sel[0][:2])[0]])
            # the sequence(s) this branch plays, copied with their action tracks aimed at the new sheet
            events = commands(tev[t["EventIndex"]]["Command"])
            new_events = []
            for c, p in events:
                if c == 2000 and len(p) == 4 and struct.unpack(">H", p[:2])[0] == 3:
                    target = struct.unpack(">H", p[2:])[0]
                    if target not in cloned:
                        q = dict(seqs[target])
                        start, n = q["ActionTrackStartIndex"], q["NumActionTracks"]
                        own = start != 65535 and any(a["TargetAcbName"] == stock_sheet for a in actions[start:start + n])
                        if not own:
                            cloned[target] = target  # plays shared BGM.acb cues: the branch reuses it
                        else:
                            q["ActionTrackStartIndex"] = len(actions)
                            for a in actions[start:start + n]:
                                a = dict(a)
                                if a["TargetAcbName"] == stock_sheet:
                                    a["TargetAcbName"] = sheet
                                actions.append(a)
                            q["ControlWorkArea1"] = len(seqs)
                            cloned[target] = len(seqs)
                            seqs.append(q)
                    p = struct.pack(">HH", 3, cloned[target])
                new_events.append((c, p))
            tev.append({"Command": encode(new_events)})
            tcmd.append({"Command": encode([(c, p[:2] + struct.pack(">H", label_i) if c == 100 and p == sel[0] else p)
                                            for c, p in cmds])})
            nt = dict(t)
            nt["EventIndex"], nt["CommandIndex"] = len(tev) - 1, len(tcmd) - 1
            tracks.append(nt)
            new_tracks.append(len(tracks) - 1)
        if new_tracks:
            # a switch sequence also lists each branch's label (string index) in TrackValues, one u16 per track:
            # the switch picks its branch there (left short, playback read past its end and crashed, 2026-10-11)
            assert len(s["TrackValues"]) == 2 * s["NumTracks"], f"sequence {s_i}: TrackValues does not match its tracks"
            s["TrackIndex"] = s["TrackIndex"] + b"".join(struct.pack(">H", i) for i in new_tracks)
            s["TrackValues"] = s["TrackValues"] + struct.pack(">H", label_i) * len(new_tracks)
            s["NumTracks"] += len(new_tracks)
    assert any(k != v for k, v in cloned.items()), "no branch of the template label reaches its sheet"
    for n, t in T.items():
        h[n] = build_utf(t)
    copied = sum(1 for k, v in cloned.items() if k != v)
    print(f"BGM.acb: label {label!r} on {sorted(selectors)}: {copied} sequences copied -> {sheet}, "
          f"{len(cloned) - copied} shared")
    return build_utf(top, 32), selectors


def rename_acb(acb: bytes, name: str) -> bytes:
    """The cue sheet's own name (top-level Name field): the sound engine registers sheets by it. Action tracks
    inside the sheet that start its own cues name the sheet too (E-Stadium's final-lap blocks start cue 47 as
    "BGM_STG1016"): they follow the rename, or the final lap played the stock sheet's song."""
    top = parse_utf(acb)
    old = top.rows[0]["Name"]
    top.rows[0]["Name"] = name
    if top.rows[0].get("ActionTrackTable"):
        actions = parse_utf(top.rows[0]["ActionTrackTable"])
        hits = 0
        for a in actions.rows:
            if a["TargetAcbName"] == old:
                a["TargetAcbName"] = name
                hits += 1
        top.rows[0]["ActionTrackTable"] = build_utf(actions)
        print(f"{name}: {hits} action tracks naming the sheet itself retargeted")
    return build_utf(top, 32)


def build(label: str, template: str, stock_sheet: str, sheet: str, sheet_acb: bytes) -> dict[str, bytes]:
    """{relative path under UNION/Content: bytes} for BGM.acb, the renamed sheet ACB and the config asset."""
    acb, selectors = add_acb_branches(BGM_ACB.read_bytes(), template, label, stock_sheet, sheet)
    uexp_old = Path(str(CONFIG) + ".uexp").read_bytes()
    uexp = add_acf_label(uexp_old, selectors, template, label)
    asset = resized(Path(str(CONFIG) + ".uasset").read_bytes(), len(uexp_old), len(uexp))
    # The ACF is the export's bulk data: the package's data-resource entry (FObjectDataResource SerialSize and
    # RawSize, int64 each) gives its size, from ACF_AT to the package tag. Left stale, the game read past the end
    # and crashed at every start (2026-10-11).
    old_bulk, new_bulk = len(uexp_old) - ACF_AT - 4, len(uexp) - ACF_AT - 4
    pair = struct.pack("<qq", old_bulk, old_bulk)
    assert asset.count(pair) == 1, "data-resource sizes not found once"
    asset = asset.replace(pair, struct.pack("<qq", new_bulk, new_bulk))
    return {"CriWareData/BGM.acb": acb,
            f"CriWareData/{sheet}.acb": rename_acb(sheet_acb, sheet),
            "01_Union/Sound/Config/UNION.uasset": asset,
            "01_Union/Sound/Config/UNION.uexp": uexp}
