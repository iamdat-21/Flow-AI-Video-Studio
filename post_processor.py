import json
import math
import os
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Optional

from database import BASE_DIR, OUTPUTS_DIR, get_conn
from flow_engine import add_log

AUDIO_LIB_DIR = os.path.join(BASE_DIR, "audio_library")
BGM_DIR = os.path.join(AUDIO_LIB_DIR, "bgm")
SFX_DIR = os.path.join(AUDIO_LIB_DIR, "sfx")
HANDOFF_DIR = os.path.join(BASE_DIR, "antigravity_jobs")

for d in (AUDIO_LIB_DIR, BGM_DIR, SFX_DIR, HANDOFF_DIR):
    os.makedirs(d, exist_ok=True)


# ==============================================================================
# 1. KHỞI TẠO THƯ VIỆN ÂM THANH MẪU (BGM & SFX) NẾU CHƯA CÓ
# ==============================================================================
def _write_sine_chord_wav(
    filepath: str,
    duration_s: float,
    freqs: list[float],
    volume: float = 0.25,
    decay: float = 0.0,
    sweep: float = 0.0,
) -> None:
    if os.path.exists(filepath):
        return
    sample_rate = 44100
    n_samples = int(sample_rate * duration_s)
    with wave.open(filepath, "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            t = i / sample_rate
            env = 1.0
            if decay > 0:
                env = math.exp(-decay * t)
            else:
                # Smooth fade in/out for BGM loops
                if t < 0.8:
                    env = t / 0.8
                elif t > duration_s - 0.8:
                    env = max(0.0, (duration_s - t) / 0.8)
            val = 0.0
            for f in freqs:
                inst_f = f + sweep * t
                val += math.sin(2.0 * math.pi * inst_f * t)
            val = (val / max(1, len(freqs))) * volume * env
            sample = max(-32767, min(32767, int(val * 32767)))
            frames += struct.pack("<hh", sample, sample)
        wf.writeframes(frames)


def init_default_audio_library() -> None:
    """Create built-in royalty-free BGM tracks & SFX samples in audio_library/ so it works out of the box."""
    # Starter BGM tracks (user can also drop any .mp3 / .wav into audio_library/bgm/)
    _write_sine_chord_wav(
        os.path.join(BGM_DIR, "warm_acoustic_lofi.wav"),
        duration_s=16.0,
        freqs=[261.63, 329.63, 392.00, 493.88],  # Cmaj7 warm pad
        volume=0.20,
    )
    _write_sine_chord_wav(
        os.path.join(BGM_DIR, "bright_commercial_upbeat.wav"),
        duration_s=16.0,
        freqs=[293.66, 369.99, 440.00, 587.33],  # D major bright pad
        volume=0.22,
    )
    _write_sine_chord_wav(
        os.path.join(BGM_DIR, "cinematic_ambient_nature.wav"),
        duration_s=16.0,
        freqs=[220.00, 261.63, 329.63, 440.00],  # Am7 cinematic pad
        volume=0.20,
    )

    # Starter SFX tracks in audio_library/sfx/
    _write_sine_chord_wav(
        os.path.join(SFX_DIR, "whoosh_transition.wav"),
        duration_s=0.65,
        freqs=[180.0, 320.0],
        volume=0.45,
        decay=4.5,
        sweep=420.0,
    )
    _write_sine_chord_wav(
        os.path.join(SFX_DIR, "pop_reveal.wav"),
        duration_s=0.35,
        freqs=[520.0, 780.0],
        volume=0.50,
        decay=10.0,
        sweep=300.0,
    )
    _write_sine_chord_wav(
        os.path.join(SFX_DIR, "sparkle_chime.wav"),
        duration_s=0.90,
        freqs=[1046.5, 1318.5, 1567.98, 2093.0],
        volume=0.38,
        decay=3.8,
    )
    _write_sine_chord_wav(
        os.path.join(SFX_DIR, "cinematic_impact.wav"),
        duration_s=1.10,
        freqs=[65.0, 98.0, 130.0],
        volume=0.60,
        decay=3.2,
    )


init_default_audio_library()


def list_audio_library() -> dict[str, list[str]]:
    bgm_files = sorted(
        [
            f
            for f in os.listdir(BGM_DIR)
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg"))
        ]
    )
    sfx_files = sorted(
        [
            f
            for f in os.listdir(SFX_DIR)
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg"))
        ]
    )
    return {"bgm": bgm_files, "sfx": sfx_files}


# ==============================================================================
# 2. TỰ ĐỘNG CHỌN SFX & BGM THEO PROMPT CẢNH (SMART AI SOUND DESIGNER)
# ==============================================================================
KEYWORD_TO_SFX = [
    (["mở", "open", "reveal", "xuất hiện", "cận cảnh", "close-up", "zoom", "dolly"], "pop_reveal.wav"),
    (["vàng ươm", "lấp lánh", "tươi ngon", "thưởng thức", "ngon", "golden", "fresh", "premium", "quà"], "sparkle_chime.wav"),
    (["mạnh", "impact", "boom", "epic", "ấn tượng", "đại cảnh"], "cinematic_impact.wav"),
]


def auto_pick_sfx_for_prompt(prompt_text: str, available_sfx: list[str]) -> Optional[str]:
    if not available_sfx:
        return None
    p_low = (prompt_text or "").lower()
    for keywords, preferred_file in KEYWORD_TO_SFX:
        if any(k in p_low for k in keywords) and preferred_file in available_sfx:
            return os.path.join(SFX_DIR, preferred_file)
    # Fallback to first available sfx if any
    return os.path.join(SFX_DIR, available_sfx[0])


def auto_pick_bgm_for_project(project_name: str, available_bgm: list[str], preferred_bgm: str = "") -> Optional[str]:
    if preferred_bgm and preferred_bgm != "auto":
        cand = os.path.join(BGM_DIR, preferred_bgm)
        if os.path.exists(cand):
            return cand
    if not available_bgm:
        return None
    p_low = (project_name or "").lower()
    if any(k in p_low for k in ("quảng cáo", "sale", "tết", "quà", "upbeat")) and "bright_commercial_upbeat.wav" in available_bgm:
        return os.path.join(BGM_DIR, "bright_commercial_upbeat.wav")
    if any(k in p_low for k in ("thiên nhiên", "nông sản", "rừng", "nature")) and "cinematic_ambient_nature.wav" in available_bgm:
        return os.path.join(BGM_DIR, "cinematic_ambient_nature.wav")
    return os.path.join(BGM_DIR, available_bgm[0])


# ==============================================================================
# 3. ENGINE HẬU KỲ FFMPEG + CẦU NỐI BẮN LỆNH CHO ANTIGRAVITY 2.0
# ==============================================================================
def probe_has_audio(video_path: str) -> bool:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def probe_duration(video_path: str) -> float:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(r.stdout.strip())
    except Exception:
        return 8.0


def normalize_scene_clip(in_mp4: str, out_mp4: str, target_w: int = 1080, target_h: int = 1920) -> bool:
    """
    Normalize a scene clip to uniform H.264 + 48kHz Stereo AAC audio
    (adds silent stereo track if the raw clip has no audio stream).
    """
    has_a = probe_has_audio(in_mp4)
    vf = f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"

    if has_a:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            in_mp4,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            out_mp4,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            in_mp4,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-shortest",
            out_mp4,
        ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return res.returncode == 0


def run_full_post_production(
    project_id: int,
    bgm_filename: str = "auto",
    enable_sfx: bool = True,
    bgm_volume: float = 0.28,
    sfx_volume: float = 0.65,
    trigger_antigravity_agent: bool = True,
) -> str:
    """
    Full automated post-production pipeline:
      1. Normalizes all completed 8s scene clips (resolution, 30fps, stereo AAC).
      2. Concatenates all 8s scenes seamlessly.
      3. Layers transition SFX (whoosh at every 8s cut) + prompt-matched action SFX from audio_library/sfx/.
      4. Mixes background music (BGM) from audio_library/bgm/ with smooth fade-in and fade-out.
      5. Emits an Antigravity 2.0 job manifest & invokes Antigravity CLI/SDK hook if installed.
    """
    with get_conn() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? AND status = 'completed' ORDER BY scene_order ASC",
            (project_id,),
        ).fetchall()

    if not proj:
        raise RuntimeError(f"Không tìm thấy Dự án #{project_id}")
    valid_scenes = [dict(s) for s in scenes if s["video_path"] and os.path.exists(s["video_path"])]
    if not valid_scenes:
        raise RuntimeError("Chưa có cảnh 8s nào hoàn thành để xử lý hậu kỳ!")

    aspect = proj["aspect_ratio"]
    target_w, target_h = (1080, 1920) if aspect == "portrait" else (1920, 1080)

    proj_dir = os.path.join(OUTPUTS_DIR, f"project_{project_id}")
    norm_dir = os.path.join(proj_dir, "normalized")
    os.makedirs(norm_dir, exist_ok=True)

    add_log(f"🎬 [Hậu Kỳ Tự Động] Bắt đầu chuẩn hóa {len(valid_scenes)} cảnh (8s/cảnh) cho Dự án [{proj['name']}]...")

    norm_paths: list[str] = []
    scene_durations: list[float] = []
    for idx, sc in enumerate(valid_scenes):
        norm_p = os.path.join(norm_dir, f"norm_scene_{idx+1:03d}.mp4")
        ok = normalize_scene_clip(sc["video_path"], norm_p, target_w=target_w, target_h=target_h)
        use_p = norm_p if ok and os.path.exists(norm_p) else sc["video_path"]
        norm_paths.append(use_p)
        scene_durations.append(probe_duration(use_p))

    # Step 2: Concatenate normalized clips
    concat_txt = os.path.join(proj_dir, "concat_norm.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in norm_paths:
            safe_p = Path(p).as_posix().replace("'", "'\\''")
            f.write(f"file '{safe_p}'\n")

    raw_concat_mp4 = os.path.join(proj_dir, "temp_concat_raw.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_txt,
            "-c",
            "copy",
            raw_concat_mp4,
        ],
        check=True,
        timeout=180,
    )

    total_duration = sum(scene_durations)
    lib = list_audio_library()
    chosen_bgm = auto_pick_bgm_for_project(proj["name"], lib["bgm"], preferred_bgm=bgm_filename)

    # Step 3: Build multi-track FFmpeg audio filtergraph (Original Audio + BGM Loop + Timed SFX)
    inputs_cmd: list[str] = ["-i", raw_concat_mp4]
    filter_parts: list[str] = ["[0:a]volume=1.0[base_a]"]
    mix_labels: list[str] = ["[base_a]"]
    input_idx = 1

    # Add BGM track (looped across total_duration with smooth fade-in 1.5s and fade-out 2.5s)
    if chosen_bgm and os.path.exists(chosen_bgm):
        inputs_cmd += ["-stream_loop", "-1", "-i", chosen_bgm]
        fade_out_st = max(0.5, total_duration - 2.5)
        filter_parts.append(
            f"[{input_idx}:a]atrim=0:{total_duration:.2f},asetpts=PTS-STARTPTS,"
            f"volume={bgm_volume:.2f},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_st:.2f}:d=2.5[bgm_a]"
        )
        mix_labels.append("[bgm_a]")
        input_idx += 1
        add_log(f"🎵 [Hậu Kỳ] Đã gắn Nhạc nền (BGM): {os.path.basename(chosen_bgm)}")

    # Add SFX tracks at scene boundaries & action timestamps
    sfx_events: list[dict[str, Any]] = []
    if enable_sfx and lib["sfx"]:
        whoosh_path = os.path.join(SFX_DIR, "whoosh_transition.wav")
        curr_time = 0.0
        for idx, sc in enumerate(valid_scenes):
            # 1) Action SFX at 1.2s into each scene based on scene prompt keywords
            act_sfx = auto_pick_sfx_for_prompt(sc["scene_prompt"], lib["sfx"])
            if act_sfx and os.path.exists(act_sfx):
                delay_ms = int((curr_time + 1.2) * 1000)
                inputs_cmd += ["-i", act_sfx]
                lbl = f"[sfx_act_{idx}]"
                filter_parts.append(
                    f"[{input_idx}:a]volume={sfx_volume:.2f},adelay={delay_ms}|{delay_ms}{lbl}"
                )
                mix_labels.append(lbl)
                input_idx += 1
                sfx_events.append({"scene": idx + 1, "time_s": round(curr_time + 1.2, 2), "sfx": os.path.basename(act_sfx)})

            # 2) Transition Whoosh SFX at scene cut (between scene idx and idx+1)
            curr_time += scene_durations[idx]
            if idx < len(valid_scenes) - 1 and os.path.exists(whoosh_path):
                trans_ms = max(0, int((curr_time - 0.25) * 1000))
                inputs_cmd += ["-i", whoosh_path]
                lbl = f"[sfx_tr_{idx}]"
                filter_parts.append(
                    f"[{input_idx}:a]volume={sfx_volume * 0.85:.2f},adelay={trans_ms}|{trans_ms}{lbl}"
                )
                mix_labels.append(lbl)
                input_idx += 1
                sfx_events.append({"scene": f"{idx+1}->{idx+2}", "time_s": round(curr_time - 0.25, 2), "sfx": "whoosh_transition.wav"})

        add_log(f"🔊 [Hậu Kỳ] Đã chèn tự động {len(sfx_events)} điểm hiệu ứng âm thanh (SFX) theo kịch bản!")

    final_mp4 = os.path.join(proj_dir, f"MASTER_POSTPROCESSED_PROJECT_{project_id}_{int(time.time())}.mp4")

    if len(mix_labels) > 1:
        amix_str = f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:dropout_transition=2:normalize=0[aout]"
        filter_complex = ";".join(filter_parts + [amix_str])
        cmd = (
            ["ffmpeg", "-y"]
            + inputs_cmd
            + [
                "-filter_complex",
                filter_complex,
                "-map",
                "0:v",
                "-map",
                "[aout]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-shortest",
                final_mp4,
            ]
        )
        subprocess.run(cmd, check=True, timeout=240)
    else:
        shutil.copyfile(raw_concat_mp4, final_mp4)

    with get_conn() as conn:
        conn.execute("UPDATE projects SET merged_video_path = ? WHERE id = ?", (final_mp4, project_id))
        conn.commit()

    # Step 4: Emit Antigravity 2.0 Handoff Manifest & Trigger Antigravity CLI/SDK if enabled
    if trigger_antigravity_agent:
        _dispatch_antigravity_handoff(
            project=dict(proj),
            scenes=valid_scenes,
            final_video_path=final_mp4,
            bgm_used=os.path.basename(chosen_bgm) if chosen_bgm else "",
            sfx_events=sfx_events,
        )

    add_log(f"🎉 [HOÀN TẤT HẬU KỲ] File Video Master (Ghép cảnh + BGM + SFX): {final_mp4}")
    return final_mp4


def _dispatch_antigravity_handoff(
    project: dict[str, Any],
    scenes: list[dict[str, Any]],
    final_video_path: str,
    bgm_used: str,
    sfx_events: list[dict[str, Any]],
) -> None:
    """
    Create an Antigravity 2.0 job manifest and trigger `google.antigravity` SDK or `agy` CLI
    if available so Antigravity 2.0 receives the post-production completion command automatically.
    """
    job_file = os.path.join(HANDOFF_DIR, f"antigravity_job_project_{project['id']}.json")
    prompt_cmd = (
        f"Antigravity 2.0 Post-Production Hook: Dự án #{project['id']} ({project['name']}) "
        f"đã tạo xong {len(scenes)} cảnh 8s và xuất bản video hậu kỳ tại '{final_video_path}' "
        f"(BGM: {bgm_used}, SFX: {len(sfx_events)} hiệu ứng). Kiểm tra chất lượng video và sẵn sàng đăng tải."
    )
    payload = {
        "project_id": project["id"],
        "project_name": project["name"],
        "total_scenes_8s": len(scenes),
        "master_video_path": final_video_path,
        "bgm_used": bgm_used,
        "sfx_events": sfx_events,
        "antigravity_command": prompt_cmd,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(job_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    add_log(f"🤖 [Antigravity 2.0 Hook] Đã bắn lệnh hậu kỳ sang Antigravity: {job_file}")

    # Optional: If google-antigravity SDK is installed, invoke it asynchronously
    try:
        from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig  # type: ignore
        import asyncio

        async def _notify_agy():
            cfg = LocalAgentConfig(
                system_instructions="You are Antigravity 2.0 video post-production supervisor.",
                capabilities=CapabilitiesConfig(),
            )
            async with Agent(cfg) as ag:
                await ag.chat(prompt_cmd)

        asyncio.run(_notify_agy())
    except Exception:
        pass
