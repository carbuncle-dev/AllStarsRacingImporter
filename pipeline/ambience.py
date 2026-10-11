"""Replace E-Stadium's stage ambience with Rocky Coaster's.

    python -m pipeline.ambience --stage stg1016 --track SAH

E-Stadium's sound level (SL_stg1016_Sound) places AtomSoundPlus emitters playing cues of
SE_OBJ_STG1016.acb (loose CRI file in pakchunk1, mounted at UNION/Content/CriWareData), whose audio
lives in the ACB's embedded memory AWB: stadium crowd/wind/tunnel/hologram/neon loops (waveforms with
LoopFlag 2) and three course-intro ambience pieces (SE_STG1016_COURSE_EXPLAIN_AMB_01-03, the last three
waveforms). Fireworks and cheer one-shots are kept.

Every loop becomes a seamless (cross-faded) 8 s piece of ASR's own track ambience (Audio.xpac
Resource/Audio/Wav/Tracks/<track>/<track>_AMB.str; SAH = Samba Hard = Rocky Coaster), each from a
different part of it; the intro pieces become ambience of their original lengths. Encoded as HCA
(EncodeType 2, encrypted with the game key + the AWB's subkey like pipeline.music) with the gain the
music gets (ASR's own music/ambience balance). The memory AWB is rebuilt in place (same total size, padded; Waveform
table rows patched: EncodeType, NumChannels, SamplingRate, NumSamples), so the ACB's layout is unchanged.
The ACB is shipped in the music pak.
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from cw.utf import parse_utf
from pipeline.music import (ASR_AUDIO, HCA_KEY, RATE, ROOT, VGAUDIO, afs2_entries, build_afs2, nested, patch)

CRI = ROOT / "extracted" / "cw" / "sound" / "UNION" / "Content" / "CriWareData"
OUT = ROOT / "out" / "music"
LOOP_SECONDS, FADE_SECONDS = 8.0, 1.0
BITRATE = 128000
INTRO_PIECES = 3  # the last waveforms: course-intro ambience


def track_ambience(track: str) -> np.ndarray:
    """ASR <track>_AMB as float stereo at RATE (extracted and decoded on first use)."""
    wav = ROOT / "extracted" / "asr" / "ambience" / f"{track}_AMB.wav"
    if not wav.exists():
        from asr.xpac import Xpac
        wav.parent.mkdir(parents=True, exist_ok=True)
        pack = Xpac(ASR_AUDIO)
        entry = next(e for e in pack.entries if e.name and e.name.endswith(f"/{track}_AMB.str"))
        src = wav.with_suffix(".xwma")
        src.write_bytes(pack.read(entry))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", str(RATE), "-ac", "2",
                        "-c:a", "pcm_s16le", str(wav)], check=True)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2).astype(np.float64) / 32768


def music_gain(song: str = "Music_SA_MAIN_A") -> float:
    """Gain pipeline.music applies to ASR's music (to TARGET_RMS_DB), for the same balance."""
    from pipeline.music import TARGET_RMS_DB, song_pcm
    x = np.frombuffer(song_pcm(song), "<i2").astype(np.float64) / 32768
    return 10.0 ** (TARGET_RMS_DB / 20.0) / float(np.sqrt(np.mean(x * x)))


def seamless(x: np.ndarray, fade: int) -> np.ndarray:
    """Loop that wraps without a click: the tail is cross-faded into the head."""
    t = np.linspace(0, 1, fade)[:, None]
    body = x[:-fade].copy()
    body[:fade] = x[:fade] * t + x[-fade:] * (1 - t)
    return body


def encode(x: np.ndarray, subkey: int, loop: bool) -> tuple[bytes, int]:
    from PyCriCodecs import HCA
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    n = len(x)
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.hca"
        with wave.open(str(src), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        args = ["-l", f"0-{n - 1}"] if loop else ["--no-loop"]
        subprocess.run([str(VGAUDIO), "-c", str(src), "-o", str(dst), *args, "--bitrate", str(BITRATE)],
                       check=True, capture_output=True)
        hca = HCA(dst.read_bytes())
    hca.encrypt(HCA_KEY, subkey)
    return hca.get_hca(), n


# ASR's balloon pop (Tracks/SAH.abc "CD_ITEM_BALLOON_DEST", sounder ITEM_BALLOON_DEST: 3D, 0 dB, distance 30-200
# ASR units = 15-100 m here), added to the sheet as its own cue. The mod plays it where a star balloon pops
# (tools/ue4ss/cpp/CustomGrandPrix: POP_CUE). Cloned from the first course-intro cue (one track, one synth,
# one waveform), with its 3D distances (sequence commands 68/69, metres) and volume (146) replaced.
POP_CUE = "SE_ASR3001_BALLOON_POP"
POP_SOUND = ("Resource/Audio/Tracks/SAH.abc", 539904, 47360)
POP_TEMPLATE = "SE_STG1016_COURSE_EXPLAIN_AMB_01"
POP_DISTANCE = (15.0, 100.0)
POP_VOLUME = 0.8


def pop_sound() -> np.ndarray:
    """ASR's balloon pop as float stereo at RATE, peak at -1 dBFS."""
    wav = ROOT / "extracted" / "asr" / "sfx" / "SAH_BALLOON_DEST.wav"
    pcm = wav.with_name(wav.stem + "_pcm.wav")
    if not pcm.exists():
        from asr.xpac import Xpac
        pack = Xpac(ASR_AUDIO)
        path, at, size = POP_SOUND
        bank = pack.read(next(e for e in pack.entries if e.name == path))
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(bank[at:at + size])  # an MS-ADPCM RIFF file
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav), "-ar", str(RATE), "-ac", "2",
                        "-c:a", "pcm_s16le", str(pcm)], check=True)
    with wave.open(str(pcm)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2).astype(np.float64) / 32768
    return x * (10 ** (-1 / 20) / max(1e-6, np.abs(x).max()))


def _commands(blob: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i + 3 <= len(blob):
        code, size = struct.unpack_from(">HB", blob, i)
        out.append((code, blob[i + 3:i + 3 + size]))
        i += 3 + size
    return out


def _encode_commands(cmds: list[tuple[int, bytes]]) -> bytes:
    return b"".join(struct.pack(">HB", c, len(a)) + a for c, a in cmds)


def add_pop_cue(acb: bytes, subkey: int) -> bytes:
    """The ACB with POP_CUE appended (new cue, sequence, track, event, synth, waveform and memory AWB file)."""
    from cw.utf import build_utf
    top = parse_utf(acb)
    h = top.rows[0]
    names = ("CueTable", "CueNameTable", "SequenceTable", "SeqCommandTable", "TrackTable", "TrackEventTable",
             "SynthTable", "WaveformTable")
    T = {n: parse_utf(h[n]) for n in names}
    cue_i = next(r["CueIndex"] for r in T["CueNameTable"].rows if r["CueName"] == POP_TEMPLATE)
    cue = dict(T["CueTable"][cue_i])
    seq = dict(T["SequenceTable"][cue["ReferenceIndex"]])
    (track_i,) = struct.unpack(">H", seq["TrackIndex"])
    track = dict(T["TrackTable"][track_i])
    event = _commands(T["TrackEventTable"][track["EventIndex"]]["Command"])
    synth_i = next(struct.unpack(">HH", a)[1] for c, a in event if c == 2000)
    synth = dict(T["SynthTable"][synth_i])
    wave_type, wave_i = struct.unpack(">HH", synth["ReferenceItems"])
    wave_row = dict(T["WaveformTable"][wave_i])

    x = pop_sound()
    hca, n = encode(x, subkey, False)
    ids, spans, align, _ = afs2_entries(h["AwbFile"])
    files = [h["AwbFile"][s:e] for s, e in spans]
    new_id = max(ids) + 1
    wave_row.update(MemoryAwbId=new_id, EncodeType=2, NumChannels=2, SamplingRate=RATE, NumSamples=n, LoopFlag=1)
    T["WaveformTable"].rows.append(wave_row)
    synth["ReferenceItems"] = struct.pack(">HH", wave_type, len(T["WaveformTable"].rows) - 1)
    T["SynthTable"].rows.append(synth)
    event = [(c, struct.pack(">HH", 2, len(T["SynthTable"].rows) - 1) if c == 2000 else a) for c, a in event]
    T["TrackEventTable"].rows.append({"Command": _encode_commands(event)})
    track["EventIndex"] = len(T["TrackEventTable"].rows) - 1
    T["TrackTable"].rows.append(track)
    replace = {68: struct.pack(">f", POP_DISTANCE[0]), 69: struct.pack(">f", POP_DISTANCE[1]),
               146: struct.pack(">f", POP_VOLUME)}
    seq_cmds = [(c, replace.get(c, a)) for c, a in _commands(T["SeqCommandTable"][seq["CommandIndex"]]["Command"])]
    T["SeqCommandTable"].rows.append({"Command": _encode_commands(seq_cmds)})
    seq.update(TrackIndex=struct.pack(">H", len(T["TrackTable"].rows) - 1),
               CommandIndex=len(T["SeqCommandTable"].rows) - 1)
    T["SequenceTable"].rows.append(seq)
    cue.update(CueId=max(r["CueId"] for r in T["CueTable"].rows) + 1,
               ReferenceIndex=len(T["SequenceTable"].rows) - 1, Length=int(round(n / RATE * 1000)))
    T["CueTable"].rows.append(cue)
    T["CueNameTable"].rows.append({"CueName": POP_CUE, "CueIndex": len(T["CueTable"].rows) - 1})
    T["CueNameTable"].rows.sort(key=lambda r: r["CueName"])  # CRI looks names up by binary search

    # memory AWB with the new file (ids ascending, as CRI looks them up)
    idsz = h["AwbFile"][6]
    ids, files = ids + [new_id], files + [hca]
    head = bytearray(h["AwbFile"][:16])
    struct.pack_into("<I", head, 8, len(files))
    head += b"".join(i.to_bytes(idsz, "little") for i in ids)
    pos, offs, body = len(head) + 4 * (len(files) + 1), [], bytearray()
    for f in files:
        offs.append(pos)
        pad = (-pos) % align
        body += bytes(pad) + f
        pos += pad + len(f)
    offs.append(pos)
    h["AwbFile"] = bytes(head) + struct.pack(f"<{len(files) + 1}I", *offs) + bytes(body)
    for n_ in names:
        h[n_] = build_utf(T[n_])
    print(f"   cue {POP_CUE} (id {cue['CueId']}): ASR balloon pop, {n / RATE:.2f} s, AWB id {new_id}")
    return build_utf(top, 32)


def build(stage: str, track: str) -> Path:
    name = f"SE_OBJ_{stage.upper()}"
    acb = bytearray((CRI / f"{name}.acb").read_bytes())
    header = parse_utf(bytes(acb))[0]
    waves = parse_utf(header["WaveformTable"])
    mem = header["AwbFile"]
    mem_at = nested(bytes(acb), header, "AwbFile")
    wave_at = nested(bytes(acb), header, "WaveformTable")
    ids, spans, _, subkey = afs2_entries(mem)
    files = [mem[s:e] for s, e in spans]
    amb = track_ambience(track)
    nw = len(waves.rows)
    loops = [i for i in range(nw) if waves[i]["LoopFlag"] == 2]
    intro = list(range(nw - INTRO_PIECES, nw))
    # Level: the stadium's own loops are encrypted (ADX type 9 / HCA) and can't be measured here, so
    # ASR's own balance is kept -- the ambience gets the gain pipeline.music gives ASR's music.
    gain = music_gain()
    seg, fade = int(LOOP_SECONDS * RATE), int(FADE_SECONDS * RATE)
    for k, i in enumerate(loops + intro):
        w = waves[i]
        is_loop = i in loops
        length = seg + fade if is_loop else int(round(w["NumSamples"] / w["SamplingRate"] * RATE))
        start = (k * len(amb) // (len(loops) + len(intro))) % max(1, len(amb) - length)
        x = amb[start:start + length] * gain
        if is_loop:
            x = seamless(x, fade)
        hca, n = encode(x, subkey, is_loop)
        files[ids.index(w["MemoryAwbId"])] = hca
        for col, val in (("EncodeType", 2), ("NumChannels", 2), ("SamplingRate", RATE), ("NumSamples", n)):
            patch(acb, wave_at, header["WaveformTable"], i, col, val)
        print(f"   waveform {i}: {'loop' if is_loop else 'intro'} {n / RATE:.1f} s from {start / RATE:.0f} s")
    new_mem = build_afs2(mem, files)
    if len(new_mem) > len(mem):
        raise SystemExit(f"new memory AWB {len(new_mem)} B > original {len(mem)} B: shorten LOOP_SECONDS")
    acb[mem_at:mem_at + len(mem)] = new_mem + bytes(len(mem) - len(new_mem))
    acb = add_pop_cue(bytes(acb), subkey)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}.acb"
    out.write_bytes(bytes(acb))
    print(f"wrote {out} (level x{gain:.2f}, memory AWB {len(new_mem)} of {len(mem)} B)")
    return out


# The custom stage's own ambience chain. The stage's sound level (SL_<stage>_Sound) places emitters playing cue
# assets (SE_ESTADIUM_*...) of the sheet asset SE_OBJ_<STAGE>, which loads CriWareData/SE_OBJ_<STAGE>.acb. The
# relocated stage borrowed E-Stadium's sound level, and the mod renamed the stock sheet's file name in memory
# while the custom cup was chosen: the sheet was often unloaded at that moment and reloaded under its stock name
# (E-Stadium's ambience, no balloon pop; 2026-10-11). Now the stage ships copies of the whole chain: the sheet
# (SE_OBJ_ASR3001, loading SE_OBJ_ASR3001.acb), the cues (SE_ -> SR_: same length; their cue names inside stay)
# and the sound level (staged under the stock name, pipeline.relocate_stage renames it with the stage).
# Same-length byte renames in the package headers (name map); the stock packages are untouched.
SOUND_CUES = ("SE_ESTADIUM_CHEER_01", "SE_ESTADIUM_CHEER_NEER_01", "SE_ESTADIUM_FIREWORKS_01", "SE_ESTADIUM_TUNNEL",
              "SE_ESTADIUM_TUNNEL_EXIT_CHEER", "SE_ESTADIUM_TUNNEL_EXIT_SILENCE", "SE_ESTADIUM_WIND_01",
              "SE_HOLOGRAM_LIGHTING_LOOP", "SE_NEONGATE_LIGHTING", "SE_NEONLIGHT_01")
CUES_EXTRACTED = ROOT / "extracted" / "cw" / "ambience_cues" / "UNION" / "Content" / "01_Union" / "Sound" / "Cue" / "SE_OBJ"
SHEETS_EXTRACTED = ROOT / "extracted" / "cw" / "alltables" / "UNION" / "Content" / "01_Union" / "Sound" / "Cue" / "SE_OBJ"
LEVELS_EXTRACTED = ROOT / "extracted" / "cw" / "stock_stage" / "UNION" / "Content" / "01_Union" / "Map" / "Stage"


def own_cue(name: str) -> str:
    return "SR_" + name[3:]


def own_sound_chain(stage: str, ours: str, cooked: Path) -> None:
    """Copies of the stage's sound level, its ambience cues and sheet into cooked (UNION/Content/...)."""
    stock_sheet, our_sheet = f"SE_OBJ_{stage.upper()}".encode(), f"SE_OBJ_{ours.upper()}".encode()
    sound = cooked / "01_Union" / "Sound" / "Cue" / "SE_OBJ"
    sound.mkdir(parents=True, exist_ok=True)
    for ext in (".uasset", ".uexp"):  # the sheet: package, object and CueSheetName (the .acb file name)
        data = (SHEETS_EXTRACTED / f"SE_OBJ_{stage.upper()}{ext}").read_bytes()
        assert stock_sheet in data
        (sound / f"SE_OBJ_{ours.upper()}{ext}").write_bytes(data.replace(stock_sheet, our_sheet))
    renames = [(c.encode(), own_cue(c).encode()) for c in sorted(SOUND_CUES, key=len, reverse=True)]
    for cue in SOUND_CUES:
        header = (CUES_EXTRACTED / f"{cue}.uasset").read_bytes()
        assert stock_sheet in header and cue.encode() in header, cue
        header = header.replace(stock_sheet, our_sheet).replace(cue.encode(), own_cue(cue).encode())
        (sound / f"{own_cue(cue)}.uasset").write_bytes(header)
        # the data keeps the cue's name in the sheet
        (sound / f"{own_cue(cue)}.uexp").write_bytes((CUES_EXTRACTED / f"{cue}.uexp").read_bytes())
    # the course intro's ambience cues (played by the intro sequence LS_<stage>): staged under their stock names,
    # pointed at our sheet; pipeline.relocate_stage renames them with the intro sequence's references to them
    for k in (1, 2, 3):
        cue = f"SE_{stage.upper()}_COURSE_EXPLAIN_AMB_{k:02d}"
        header = (CUES_EXTRACTED / f"{cue}.uasset").read_bytes()
        assert stock_sheet in header, cue
        (sound / f"{cue}.uasset").write_bytes(header.replace(stock_sheet, our_sheet))
        (sound / f"{cue}.uexp").write_bytes((CUES_EXTRACTED / f"{cue}.uexp").read_bytes())
    level = LEVELS_EXTRACTED / stage / f"SL_{stage}_Sound"
    out = cooked / "01_Union" / "Map" / "Stage" / stage
    out.mkdir(parents=True, exist_ok=True)
    header = level.with_suffix(".umap").read_bytes()
    for old, new in renames:  # longest first: SE_ESTADIUM_TUNNEL is a prefix of two others
        header = header.replace(b"/SE_OBJ/" + old + b"\0", b"/SE_OBJ/" + new + b"\0").replace(
            len(old + b"\0").to_bytes(4, "little") + old + b"\0", len(new + b"\0").to_bytes(4, "little") + new + b"\0")
    left = [c for c in SOUND_CUES if c.encode() + b"\0" in header]
    assert not left, f"sound level still names {left}"
    (out / f"SL_{stage}_Sound.umap").write_bytes(header)
    (out / f"SL_{stage}_Sound.uexp").write_bytes(level.with_suffix(".uexp").read_bytes())
    print(f"own ambience chain: SL_{stage}_Sound (renamed with the stage), {len(SOUND_CUES)} cues SR_*, sheet SE_OBJ_{ours.upper()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--track", default="SAH")
    args = ap.parse_args()
    build(args.stage, args.track)


if __name__ == "__main__":
    main()
