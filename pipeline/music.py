"""Replace a CrossWorlds stage's music with All-Stars Racing songs (CRI ADX2 cue sheet + wave bank).

    python -m pipeline.music --stage stg1016 --song Music_SA_MAIN_A --name RockyCoaster

The stage's BGM_<STAGE>.acb / .awb (loose files in pakchunk1, mounted at UNION/Content/CriWareData)
are patched in place -- every field touched has a fixed size, so no @UTF table is rewritten:
  * the stage's three songs (main, intro, final lap; E-Stadium waveforms 0, 3, 4) become ASR songs,
    encoded as HCA (encrypted with the game's key) that loops the whole song like ASR does: Waveform.NumSamples and the
    loop points in WaveformExtensionData follow the new length
  * race cues whose block sequences only layer E-Stadium stems over the main song are pointed at
    the plain sequence that plays one song (the jukebox one); cues whose blocks also trigger other
    cues (final lap: announcer, final-lap song) are kept and their E-Stadium stingers silenced
  * songs are brought to the game's loudness (gain + look-ahead limiter); the course intro plays
    the ASR intro jingle once, then the main song
  * the stream AWB is rebuilt (AFS2: same ids/alignment/subkey), its 224-byte header copy in the ACB
    (StreamAwbAfs2Header) and MD5 (StreamAwbHash) updated; waveforms streamed with a memory prefetch
    get the first bytes of their new stream in the ACB's memory AWB (same size as before)
The result is packed with UnrealPak into out/mods/zzz_<name>_Music_P.pak.

Song files come from ASR's Audio.xpac (Resource/Audio/Wav/Music/*.str, xWMA) decoded with ffmpeg.
Which ASR song belonged to a track is not stored anywhere: a zone's songs are picked by the jukebox.
"""

from __future__ import annotations

import os
import argparse
import hashlib
import struct
import subprocess
import wave
from pathlib import Path

from cw.utf import field_offset, parse_utf

ROOT = Path(__file__).resolve().parent.parent
CRI = ROOT / "extracted" / "cw" / "criware"
MUSIC = ROOT / "extracted" / "asr" / "music"
ASR_AUDIO = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Sonic and SEGA All Stars Racing\Resource\Audio.xpac")
UNREALPAK = Path(os.environ.get("UE_ENGINE_DIR", Path.home() / "UE5-SRCW" / "Windows")) \
    / "Engine" / "Binaries" / "Win64" / "UnrealPak.exe"
MOUNT = "../../../UNION/Content/CriWareData/"
RATE = 48000
BITRATE = 256000  # the game's BGM: 0x2AA-byte frames per 1024 samples at 48 kHz
VGAUDIO = ROOT / "tools" / "bin" / "VGAudioCli.exe"  # github.com/Thealexbarney/VGAudio v2.2.1
HCA_KEY = 0x00720FB46101DF7A  # Sonic Racing CrossWorlds (vgmstream hca_keys.h); verified on BGM_STG1016

# E-Stadium: waveform index -> role ("silence" keeps the length); cue index -> plain sequence it is
# redirected to. Only cues whose block sequence has no action tracks are redirected: BGM_ESTADIUM_3
# (final lap) starts the final-lap song, shared BGM cues and the "final lap" announcer
# (VOICE_ANNOUNCE) from its blocks, so it keeps its blocks and only its two E-Stadium stingers
# (waveforms 1 and 2) are silenced.
# Final lap: block 3 plays waveform 1 (3 s) while the announcer speaks, then block 4 starts the
# final-lap song cue (waveform 4) at 3000 ms -- waveform 1 is the song's first 3 s ("final_lead") and
# waveform 4 the rest of it, wrapping round to those 3 s, so the song runs through the transition.
WAVES = {"stg1016": {0: "main", 3: "intro", 1: "final_lead", 4: "final", 2: "silence"}}
FINAL_LEAD = {"stg1016": 1}  # waveform whose length the final-lap song starts with
REROUTE = {"stg1016": {0: 11,   # BGM_ESTADIUM_1 (main song + stems) -> BGM_LISTEN_STG1016 (main song)
                       4: 11}}  # BGM_ESTADIUM_2 -> main song
SEQUENCE_REF = 3
# The game's BGM is mastered loud (E-Stadium: -11 dBFS gated RMS, peaks at full scale); ASR's songs
# sit near -17 dBFS. Songs get gain to this level, peaks held under LIMIT by a look-ahead limiter.
TARGET_RMS_DB = -8.0  # above E-Stadium (-11): sounded quiet next to other courses in game
FINAL_BOOST_DB = 1.5  # E-Stadium's final-lap song is mastered 1.4 dB above its main song
LIMIT = 0.97


def afs2_entries(d: bytes) -> tuple[list[int], list[tuple[int, int]], int, int]:
    """(ids, [(start, end)] of each file, alignment, subkey) of an AFS2 archive."""
    if d[:4] != b"AFS2" or d[5] != 4 or d[6] not in (2, 4):
        raise ValueError("unsupported AFS2 layout")
    idsz, n = d[6], struct.unpack_from("<I", d, 8)[0]
    align, subkey = struct.unpack_from("<HH", d, 12)
    ids = [int.from_bytes(d[16 + i * idsz:16 + (i + 1) * idsz], "little") for i in range(n)]
    base = 16 + idsz * n
    offs = struct.unpack_from(f"<{n + 1}I", d, base)
    return ids, [((offs[i] + align - 1) // align * align, offs[i + 1]) for i in range(n)], align, subkey


def build_afs2(template: bytes, files: list[bytes]) -> bytes:
    """AFS2 with the template's header fields and ids, and new file contents."""
    ids, _, align, subkey = afs2_entries(template)
    idsz, n = template[6], len(files)
    header_size = 16 + idsz * n + 4 * (n + 1)
    head = bytearray(template[:16 + idsz * n])
    offs, pos, body = [], header_size, bytearray()
    for f in files:
        offs.append(pos)
        pad = (-pos) % align
        body += bytes(pad) + f
        pos += pad + len(f)
    offs.append(pos)
    return bytes(head) + struct.pack(f"<{n + 1}I", *offs) + bytes(body)


def song_pcm(name: str) -> bytes:
    """16-bit stereo 48 kHz PCM of an ASR music file (extracted and decoded on first use)."""
    wav = MUSIC / f"{name}.wav"
    if not wav.exists():
        from asr.xpac import Xpac
        MUSIC.mkdir(parents=True, exist_ok=True)
        pack = Xpac(ASR_AUDIO)
        entry = next(e for e in pack.entries if e.name and e.name.endswith(f"/{name}.str"))
        src = MUSIC / f"{name}.xwma"
        src.write_bytes(pack.read(entry))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ar", str(RATE), "-ac", "2",
                        "-c:a", "pcm_s16le", str(wav)], check=True)
    with wave.open(str(wav)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (2, 2, RATE):
            raise ValueError(f"{wav}: expected 16-bit stereo {RATE} Hz")
        return w.readframes(w.getnframes())


def loudness(pcm: bytes, boost_db: float = 0.0) -> bytes:
    """Gain to TARGET_RMS_DB (gated RMS over 400 ms blocks) with a 5 ms look-ahead peak limiter."""
    import numpy as np
    x = np.frombuffer(pcm, np.int16).reshape(-1, 2).astype(np.float64) / 32768.0
    blocks = x[:len(x) // 19200 * 19200].reshape(-1, 19200, 2)
    rms = np.sqrt((blocks ** 2).mean(axis=(1, 2)))
    if not rms.size or rms.max() == 0.0:
        return pcm
    gated = rms[rms > rms.max() * 0.1]
    x *= 10.0 ** ((TARGET_RMS_DB + boost_db) / 20.0) / np.sqrt((gated ** 2).mean())
    peak = np.abs(x).max(axis=1)
    win = 240  # 5 ms
    padded = np.concatenate([peak, np.zeros(win)])
    env = np.lib.stride_tricks.sliding_window_view(padded, win + 1).max(axis=1)[:len(peak)]  # max over the next 5 ms
    gain = np.minimum(1.0, LIMIT / np.maximum(env, 1e-9))
    kernel = np.ones(win) / win  # smooth the gain (attack within the look-ahead window)
    gain = np.minimum(gain, np.convolve(np.concatenate([gain, np.ones(win - 1)]), kernel, "valid")[:len(gain)])
    # release: gain recovers over ~50 ms
    out = gain.copy()
    rel = np.exp(-1.0 / (0.05 * RATE))
    for i in range(1, len(out)):
        out[i] = min(gain[i], out[i - 1] * rel + (1.0 - rel))
    y = np.clip(x * out[:, None], -LIMIT, LIMIT)
    return (y * 32767.0).astype(np.int16).tobytes()


def encode_loop(pcm: bytes, subkey: int, loop_start: int | None = 0) -> tuple[bytes, int]:
    """HCA looping from loop_start to the end (None: no loop), encrypted like the game's (type 56, HCA_KEY + the AWB's subkey).

    The game decrypts its BGM with its key whatever the header says: an unencrypted stream played
    as noise. Encoded with VGAudio (PyCriCodecs 0.4.9 only does the encryption).
    """
    from PyCriCodecs import HCA
    import tempfile
    n = len(pcm) // 4
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.wav", Path(tmp) / "out.hca"
        with wave.open(str(src), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        loop = ["--no-loop"] if loop_start is None else ["-l", f"{loop_start}-{n - 1}"]
        subprocess.run([str(VGAUDIO), "-c", str(src), "-o", str(dst), *loop, "--bitrate", str(BITRATE)],
                       check=True, capture_output=True)
        hca = HCA(dst.read_bytes())
    hca.encrypt(HCA_KEY, subkey)
    return hca.get_hca(), n


def nested(acb: bytes, header: dict, table: str) -> int:
    """Absolute offset of a nested table (unique bytes) inside the ACB."""
    blob = header[table]
    at = acb.find(blob)
    if at < 0 or acb.find(blob, at + 1) >= 0:
        raise ValueError(f"{table} not found once in the ACB")
    return at


def patch(acb_table: bytearray, base: int, blob: bytes, row: int, column: str, value: int) -> None:
    off, fmt = field_offset(blob, row, column)
    struct.pack_into(fmt, acb_table, base + off, value)


def build(stage: str, songs: dict[str, str]) -> tuple[bytes, bytes]:
    name = f"BGM_{stage.upper()}"
    acb = bytearray((CRI / f"{name}.acb").read_bytes())
    awb = (CRI / f"{name}.awb").read_bytes()
    header = parse_utf(bytes(acb))[0]
    waves = parse_utf(header["WaveformTable"]).rows
    ext_rows = parse_utf(header["WaveformExtensionDataTable"]).rows
    ids, spans, _, subkey = afs2_entries(awb)
    stream_files = [awb[s:e] for s, e in spans]
    mem = header["AwbFile"]
    mem_at = nested(bytes(acb), header, "AwbFile")
    mem_ids, mem_spans, _, _ = afs2_entries(mem)
    wave_at = nested(bytes(acb), header, "WaveformTable")
    ext_at = nested(bytes(acb), header, "WaveformExtensionDataTable")
    for index, role in WAVES[stage].items():
        w = waves[index]
        if role == "silence":
            pcm, start, label = bytes(4 * w["NumSamples"]), (0 if w["LoopFlag"] == 2 else None), "silence"
        elif role == "intro":
            # the intro jingle once, then the main song looping (the course intro outlasts the jingle)
            jingle = song_pcm(songs["intro"])
            pcm, start = loudness(jingle + song_pcm(songs["main"])), len(jingle) // 4
            label = f"{songs['intro']} then {songs['main']}"
        elif role in ("final_lead", "final"):
            song = loudness(song_pcm(songs["final"]), FINAL_BOOST_DB)
            lead = 4 * waves[FINAL_LEAD[stage]]["NumSamples"]
            pcm = song[:lead] if role == "final_lead" else song[lead:] + song[:lead]
            start = 0 if w["LoopFlag"] == 2 else None
            label = f"{songs['final']} ({'first' if role == 'final_lead' else 'after'} {lead // 4 / RATE:.1f} s)"
        else:
            pcm, start, label = loudness(song_pcm(songs[role])), 0, songs[role]
        hca, n = encode_loop(pcm, subkey, start)
        stream_files[ids.index(w["StreamAwbId"])] = hca
        if w["Streaming"] == 2:  # memory prefetch: the start of the stream, same length as before
            s, e = mem_spans[mem_ids.index(w["MemoryAwbId"])]
            acb[mem_at + s:mem_at + e] = hca[:e - s]
        elif w["Streaming"] != 1:
            raise ValueError(f"waveform {index} is not streamed")
        patch(acb, wave_at, header["WaveformTable"], index, "NumSamples", n)
        if w["LoopFlag"] == 2 and w["ExtensionData"] < len(ext_rows):
            patch(acb, ext_at, header["WaveformExtensionDataTable"], w["ExtensionData"], "LoopStart", start or 0)
            patch(acb, ext_at, header["WaveformExtensionDataTable"], w["ExtensionData"], "LoopEnd", n)
        print(f"   waveform {index} ({role}): {label}, {n / RATE:.1f} s")
    cue_at = nested(bytes(acb), header, "CueTable")
    names = {r["CueIndex"]: r["CueName"] for r in parse_utf(header["CueNameTable"]).rows}
    for cue, sequence in REROUTE[stage].items():
        patch(acb, cue_at, header["CueTable"], cue, "ReferenceType", SEQUENCE_REF)
        patch(acb, cue_at, header["CueTable"], cue, "ReferenceIndex", sequence)
        print(f"   cue {names[cue]} -> sequence {sequence}")
    new_awb = build_afs2(awb, stream_files)
    old_head = parse_utf(header["StreamAwbAfs2Header"])[0]["Header"]
    new_head = new_awb[:len(old_head)]
    head_at = acb.find(old_head)
    hash_old = parse_utf(header["StreamAwbHash"])[0]["Hash"]
    hash_at = acb.find(hash_old)
    if head_at < 0 or hash_at < 0 or len(new_head) != len(old_head):
        raise ValueError("stream AWB header/hash not found in the ACB")
    acb[head_at:head_at + len(new_head)] = new_head
    acb[hash_at:hash_at + 16] = hashlib.md5(new_awb).digest()
    return bytes(acb), new_awb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--name", default="RockyCoaster")
    ap.add_argument("--song", default="Music_SA_MAIN_A", help="ASR song for the race (main and final lap)")
    ap.add_argument("--final", help="different ASR song for the final lap")
    ap.add_argument("--intro", default="Music_SA_INTRO", help="ASR song for the course intro")
    args = ap.parse_args()
    acb, awb = build(args.stage, {"main": args.song, "final": args.final or args.song, "intro": args.intro})
    stage_dir = ROOT / "out" / "music" / "CriWareData"
    stage_dir.mkdir(parents=True, exist_ok=True)
    name = f"BGM_{args.stage.upper()}"
    (stage_dir / f"{name}.acb").write_bytes(acb)
    (stage_dir / f"{name}.awb").write_bytes(awb)
    response = stage_dir.parent / "files.txt"
    response.write_text("".join(f'"{(stage_dir / f).resolve()}" "{MOUNT}{f}"\n' for f in (f"{name}.acb", f"{name}.awb")))
    pak = ROOT / "out" / "mods" / f"zzz_{args.name}_Music_P.pak"
    pak.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(UNREALPAK), str(pak), f"-create={response}"], check=True, capture_output=True)
    print(f"wrote {pak} ({pak.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
