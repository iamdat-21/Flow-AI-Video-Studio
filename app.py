import asyncio
import collections
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from database import (
    OUTPUTS_DIR,
    PROFILES_DIR,
    UPLOADS_DIR,
    compose_scene_prompt,
    get_conn,
    init_db,
)
from flask import Flask, jsonify, request, send_from_directory
from flow_engine import (
    LIVE_LOGS,
    CreditExhaustedError,
    FlowAccountRunner,
    add_log,
    check_proxy_ip,
    merge_project_scenes_ffmpeg,
)
from post_processor import (
    BGM_DIR,
    SFX_DIR,
    list_audio_library,
    run_full_post_production,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
init_db()

# Track background worker state
WORKER_STATE = {"running": False, "project_id": None}


def save_uploaded_file(file_storage: Any, prefix: str = "ref") -> str:
    if not file_storage or not file_storage.filename:
        return ""
    safe_name = secure_filename(file_storage.filename)
    fname = f"{prefix}_{int(time.time() * 1000)}_{safe_name}"
    out_path = os.path.join(UPLOADS_DIR, fname)
    file_storage.save(out_path)
    return out_path


# ==============================================================================
# API: ACCOUNTS & PROXY IP MANAGEMENT (1 ACC = 1 IP RIÊNG)
# ==============================================================================
@app.route("/api/accounts", methods=["GET"])
def api_list_accounts():
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()]
    return jsonify({"ok": True, "accounts": rows})


@app.route("/api/accounts", methods=["POST"])
def api_add_account():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    proxy_url = (data.get("proxy_url") or "").strip()
    flow_url = (data.get("flow_project_url") or "https://flow.google.com/").strip()

    if not name:
        return jsonify({"ok": False, "error": "Vui lòng nhập tên tài khoản!"}), 400

    with get_conn() as conn:
        # Enforce 1 Account = 1 Unique Proxy IP check
        if proxy_url:
            dup = conn.execute(
                "SELECT name FROM accounts WHERE proxy_url = ?", (proxy_url,)
            ).fetchone()
            if dup:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": f"Proxy IP này đã được gán cho tài khoản [{dup['name']}]! Mỗi tài khoản phải chạy trên 1 IP khác nhau.",
                        }
                    ),
                    400,
                )

        cur = conn.execute(
            "INSERT INTO accounts (name, email, proxy_url, profile_dir, flow_project_url) VALUES (?, ?, ?, '', ?)",
            (name, email, proxy_url, flow_url),
        )
        acc_id = cur.lastrowid
        prof_dir = os.path.join(PROFILES_DIR, f"acc_{acc_id}")
        os.makedirs(prof_dir, exist_ok=True)
        conn.execute("UPDATE accounts SET profile_dir = ? WHERE id = ?", (prof_dir, acc_id))
        conn.commit()

    add_log(f"➕ Đã thêm tài khoản [{name}] với Profile riêng: acc_{acc_id} | Proxy: {proxy_url or 'Direct IP'}")
    return jsonify({"ok": True, "id": acc_id})


@app.route("/api/accounts/<int:acc_id>", methods=["DELETE"])
def api_delete_account(acc_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
        conn.commit()
    add_log(f"🗑️ Đã xóa tài khoản #{acc_id}")
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:acc_id>/check-ip", methods=["POST"])
def api_check_account_ip(acc_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy tài khoản"}), 404

    res = check_proxy_ip(row["proxy_url"])
    if res["ok"]:
        ip_val = res["ip"]
        with get_conn() as conn:
            # Check if another account has the same resolved IP
            conflict = conn.execute(
                "SELECT name FROM accounts WHERE last_ip = ? AND id != ? AND last_ip != ''",
                (ip_val, acc_id),
            ).fetchone()
            conn.execute("UPDATE accounts SET last_ip = ? WHERE id = ?", (ip_val, acc_id))
            conn.commit()
        warn_msg = f" (⚠️ CẢNH BÁO: Trùng IP với {conflict['name']}!)" if conflict else ""
        add_log(f"🌐 Kiểm tra IP Acc [{row['name']}] -> {ip_val}{warn_msg}")
        return jsonify({"ok": True, "ip": ip_val, "conflict": conflict["name"] if conflict else None})
    else:
        add_log(f"❌ Lỗi kết nối Proxy của Acc [{row['name']}]: {res.get('error')}")
        return jsonify({"ok": False, "error": res.get("error")}), 400


@app.route("/api/accounts/<int:acc_id>/login", methods=["POST"])
def api_open_account_login(acc_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy tài khoản"}), 404

    def _bg():
        runner = FlowAccountRunner(dict(row))
        asyncio.run(runner.open_for_manual_login())

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "message": "Đã mở trình duyệt đăng nhập cho tài khoản!"})


@app.route("/api/accounts/<int:acc_id>/update", methods=["POST"])
def api_update_account(acc_id: int):
    data = request.json or {}
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (acc_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Không tìm thấy tài khoản"}), 404
        new_url = (data.get("flow_project_url") or row["flow_project_url"]).strip()
        new_status = (data.get("status") or row["status"]).strip()
        new_credits = data.get("credits_info", row["credits_info"])
        conn.execute(
            "UPDATE accounts SET flow_project_url = ?, status = ?, credits_info = ? WHERE id = ?",
            (new_url, new_status, new_credits, acc_id),
        )
        conn.commit()
    add_log(f"🔄 Đã cập nhật Acc [{row['name']}]: Trạng thái={new_status} | Link={new_url}")
    return jsonify({"ok": True})


# ==============================================================================
# API: PROJECTS & 8-SECOND SCENES (REF MEDIA + REF DESCRIPTION + PROMPT)
# ==============================================================================
@app.route("/api/projects", methods=["GET"])
def api_list_projects():
    with get_conn() as conn:
        projs = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()]
        for p in projs:
            scenes = [
                dict(s)
                for s in conn.execute(
                    "SELECT * FROM scenes WHERE project_id = ? ORDER BY scene_order ASC, id ASC",
                    (p["id"],),
                ).fetchall()
            ]
            p["scenes"] = scenes
    return jsonify({"ok": True, "projects": projs, "worker": WORKER_STATE})


@app.route("/api/projects", methods=["POST"])
def api_create_project():
    name = (request.form.get("name") or "").strip()
    aspect_ratio = request.form.get("aspect_ratio") or "portrait"
    model_name = request.form.get("model_name") or "Omni 1.1 Flash"
    video_count = request.form.get("video_count") or "x1"
    confirm_mode = request.form.get("confirm_mode") or "never"
    gen_mode = request.form.get("gen_mode") or "references"
    global_ref_desc = (request.form.get("global_ref_desc") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "Vui lòng nhập tên dự án video!"}), 400

    ref_file = request.files.get("global_ref_media")
    ref_path = save_uploaded_file(ref_file, prefix="proj_ref") if ref_file else ""

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO projects (name, aspect_ratio, model_name, video_count, confirm_mode, gen_mode, global_ref_media, global_ref_desc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, aspect_ratio, model_name, video_count, confirm_mode, gen_mode, ref_path, global_ref_desc),
        )
        pid = cur.lastrowid
        conn.commit()

    add_log(
        f"📁 Đã tạo Dự án mới #{pid}: [{name}] (Khung: {aspect_ratio.upper()} | Model: {model_name} | Số lượng: {video_count} | Xác nhận: {confirm_mode.upper()})"
    )
    return jsonify({"ok": True, "project_id": pid})


@app.route("/api/projects/<int:project_id>/settings", methods=["POST"])
def api_update_project_settings(project_id: int):
    data = request.json or {}
    with get_conn() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            return jsonify({"ok": False, "error": "Không tìm thấy dự án"}), 404
        aspect_ratio = data.get("aspect_ratio") or proj["aspect_ratio"]
        model_name = data.get("model_name") or proj["model_name"]
        video_count = data.get("video_count") or proj.get("video_count") or "x1"
        confirm_mode = data.get("confirm_mode") or proj.get("confirm_mode") or "never"
        gen_mode = data.get("gen_mode") or proj["gen_mode"]
        conn.execute(
            """UPDATE projects
               SET aspect_ratio = ?, model_name = ?, video_count = ?, confirm_mode = ?, gen_mode = ?
               WHERE id = ?""",
            (aspect_ratio, model_name, video_count, confirm_mode, gen_mode, project_id),
        )
        conn.commit()
    add_log(
        f"⚙️ Đã cập nhật 'Cài đặt tác nhân' cho Dự án #{project_id}: Khung={'9:16 Dọc' if aspect_ratio == 'portrait' else '16:9 Ngang'} | {video_count} | {model_name} | Xác nhận={confirm_mode}"
    )
    return jsonify({"ok": True})


@app.route("/api/projects/<int:project_id>/scenes", methods=["POST"])
def api_add_scene(project_id: int):
    scene_prompt = (request.form.get("scene_prompt") or "").strip()
    ref_description = (request.form.get("ref_description") or "").strip()
    assigned_acc = request.form.get("assigned_account_id")
    assigned_acc_id = int(assigned_acc) if assigned_acc and assigned_acc.isdigit() else None

    if not scene_prompt:
        return jsonify({"ok": False, "error": "Vui lòng nhập Prompt cho cảnh 8s!"}), 400

    ref_file = request.files.get("ref_media")
    ref_path = save_uploaded_file(ref_file, prefix=f"scene_p{project_id}") if ref_file else ""

    with get_conn() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            return jsonify({"ok": False, "error": "Không tìm thấy dự án"}), 404

        max_ord = conn.execute(
            "SELECT COALESCE(MAX(scene_order), 0) FROM scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        next_ord = int(max_ord) + 1

        composed = compose_scene_prompt(
            scene_prompt=scene_prompt,
            ref_description=ref_description,
            global_ref_desc=proj["global_ref_desc"],
            duration_sec=8,
        )

        conn.execute(
            """INSERT INTO scenes
               (project_id, scene_order, duration_sec, ref_media_path, ref_description, scene_prompt, composed_prompt, assigned_account_id, status)
               VALUES (?, ?, 8, ?, ?, ?, ?, ?, 'pending')""",
            (project_id, next_ord, ref_path, ref_description, scene_prompt, composed, assigned_acc_id),
        )
        conn.commit()

    add_log(f"🎞️ Đã thêm Cảnh #{next_ord} (8s) vào Dự án [{proj['name']}]")
    return jsonify({"ok": True})


@app.route("/api/projects/<int:project_id>/scenes/bulk", methods=["POST"])
def api_bulk_add_scenes(project_id: int):
    data = request.json or {}
    lines = [ln.strip() for ln in (data.get("prompts_text") or "").splitlines() if ln.strip()]
    ref_description = (data.get("ref_description") or "").strip()

    if not lines:
        return jsonify({"ok": False, "error": "Danh sách Prompt trống!"}), 400

    with get_conn() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not proj:
            return jsonify({"ok": False, "error": "Không tìm thấy dự án"}), 404

        max_ord = conn.execute(
            "SELECT COALESCE(MAX(scene_order), 0) FROM scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        next_ord = int(max_ord)

        for ln in lines:
            next_ord += 1
            composed = compose_scene_prompt(
                scene_prompt=ln,
                ref_description=ref_description,
                global_ref_desc=proj["global_ref_desc"],
                duration_sec=8,
            )
            conn.execute(
                """INSERT INTO scenes
                   (project_id, scene_order, duration_sec, ref_media_path, ref_description, scene_prompt, composed_prompt, status)
                   VALUES (?, ?, 8, '', ?, ?, ?, 'pending')""",
                (project_id, next_ord, ref_description, ln, composed),
            )
        conn.commit()

    add_log(f"📚 Đã thêm hàng loạt {len(lines)} cảnh (mỗi cảnh 8s) vào Dự án [{proj['name']}]")
    return jsonify({"ok": True, "added": len(lines)})


@app.route("/api/scenes/<int:scene_id>", methods=["DELETE"])
def api_delete_scene(scene_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM scenes WHERE id = ?", (scene_id,))
        conn.commit()
    return jsonify({"ok": True})


# ==============================================================================
# API: MULTI-ACCOUNT / MULTI-IP BATCH EXECUTION & FFMPEG STITCHING
# ==============================================================================
@app.route("/api/projects/<int:project_id>/run", methods=["POST"])
def api_run_project(project_id: int):
    if WORKER_STATE["running"]:
        return jsonify({"ok": False, "error": "Hệ thống đang chạy một tiến trình tạo video khác!"}), 400

    data = request.json or {}
    parallel_workers = max(1, min(5, int(data.get("parallel_workers", 2))))
    auto_postprocess = bool(data.get("auto_postprocess", True))
    bgm_filename = data.get("bgm_filename", "auto")
    enable_sfx = bool(data.get("enable_sfx", True))
    headless_mode = bool(data.get("headless_mode", True))

    with get_conn() as conn:
        proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        all_accounts = [dict(a) for a in conn.execute("SELECT * FROM accounts ORDER BY id ASC").fetchall()]
        accounts = [a for a in all_accounts if a.get("status") != "out_of_credits"]
        scenes = [
            dict(s)
            for s in conn.execute(
                "SELECT * FROM scenes WHERE project_id = ? AND status != 'completed' ORDER BY scene_order ASC",
                (project_id,),
            ).fetchall()
        ]

    if not proj:
        return jsonify({"ok": False, "error": "Không tìm thấy dự án!"}), 404
    if not all_accounts:
        return jsonify({"ok": False, "error": "Chưa có Tài khoản Flow nào! Hãy thêm ít nhất 1 Tài khoản ở mục Quản lý Acc."}), 400
    if not accounts:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Tất cả các tài khoản hiện tại đều đang ở trạng thái 🚫 HẾT TÍN DỤNG! Hãy thêm tài khoản mới hoặc bấm nút '🔄 Reset Tín Dụng' trên tài khoản đã nạp thêm.",
                }
            ),
            400,
        )
    if not scenes:
        return jsonify({"ok": False, "error": "Tất cả các cảnh 8s trong dự án này đã hoàn thành!"}), 400

    # Verify Unique Proxy IPs across active accounts if > 1 account
    seen_proxies = set()
    for a in accounts:
        px = (a.get("proxy_url") or "").strip()
        if len(accounts) > 1 and px in seen_proxies:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Phát hiện trùng cấu hình Proxy ({px or 'Direct IP'}) giữa các tài khoản! Mỗi tài khoản bắt buộc phải có 1 IP riêng.",
                    }
                ),
                400,
            )
        seen_proxies.add(px)

    def _run_batch_job():
        WORKER_STATE["running"] = True
        WORKER_STATE["project_id"] = project_id
        add_log(
            f"🚀 BẮT ĐẦU CHẠY DỰ ÁN [{proj['name']}]: {len(scenes)} cảnh (8s/cảnh) trên {len(accounts)} Tài khoản còn tín dụng (Đa IP độc lập + Tự động đổi Acc khi hết tín dụng)..."
        )

        q_lock = threading.Lock()
        scene_queue: collections.deque[dict[str, Any]] = collections.deque(scenes)
        exhausted_acc_ids: set[int] = set()

        def _pop_next_scene_for_account(acc_id: int) -> Any:
            with q_lock:
                if not scene_queue:
                    return None
                # Prefer scene explicitly assigned to this acc_id, or unassigned, or assigned to an already-exhausted/non-active account
                active_ids = {a["id"] for a in accounts if a["id"] not in exhausted_acc_ids}
                for idx, sc in enumerate(scene_queue):
                    pref = sc.get("assigned_account_id")
                    if not pref or pref == acc_id or pref not in active_ids:
                        del scene_queue[idx]
                        return sc
                # Otherwise take the first available scene so no scene is ever stranded
                return scene_queue.popleft()

        def _worker_for_account(acc_row: dict[str, Any]):
            acc_id = acc_row["id"]
            runner = FlowAccountRunner(acc_row)
            while True:
                if acc_id in exhausted_acc_ids:
                    break
                sc = _pop_next_scene_for_account(acc_id)
                if not sc:
                    break
                sc_id = sc["id"]
                with get_conn() as c:
                    c.execute(
                        "UPDATE scenes SET status = 'running', assigned_account_id = ?, error_msg = '' WHERE id = ?",
                        (acc_id, sc_id),
                    )
                    c.commit()
                try:
                    out_mp4 = asyncio.run(
                        runner.generate_scene_8s(sc, dict(proj), headless_mode=headless_mode)
                    )
                    with get_conn() as c:
                        c.execute(
                            "UPDATE scenes SET status = 'completed', video_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (out_mp4, sc_id),
                        )
                        c.commit()
                except CreditExhaustedError as ce:
                    with q_lock:
                        exhausted_acc_ids.add(acc_id)
                        # Push this scene back to the front of the queue so the next account picks it up!
                        sc["assigned_account_id"] = None
                        scene_queue.appendleft(sc)
                    with get_conn() as c:
                        c.execute(
                            "UPDATE scenes SET status = 'pending', assigned_account_id = NULL, error_msg = ? WHERE id = ?",
                            (f"Đang chuyển sang Acc khác (do {acc_row['name']} hết tín dụng)...", sc_id),
                        )
                        c.commit()
                    add_log(
                        f"⚠️ [TỰ ĐỘNG ĐỔI TÀI KHOẢN] {ce} -> Đã khóa tạm [{acc_row['name']}] và chuyển Cảnh #{sc['scene_order']} sang Tài khoản + IP dự phòng tiếp theo!"
                    )
                    break
                except Exception as e:
                    err_str = str(e)
                    add_log(f"❌ Lỗi ở Cảnh #{sc['scene_order']} trên Acc [{acc_row['name']}]: {err_str}")
                    with get_conn() as c:
                        c.execute(
                            "UPDATE scenes SET status = 'failed', error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (err_str, sc_id),
                        )
                        c.commit()

        # Run across accounts; if parallel_workers < len(accounts), as soon as 1 account finishes/exhausts, the next account in the pool starts!
        actual_workers = min(parallel_workers, len(accounts))
        with ThreadPoolExecutor(max_workers=actual_workers) as ex:
            futures = [ex.submit(_worker_for_account, a) for a in accounts]
            for f in futures:
                try:
                    f.result()
                except Exception as exc:
                    add_log(f"⚠️ Worker exception: {exc}")

        if scene_queue and len(exhausted_acc_ids) >= len(accounts):
            add_log(
                f"🚫 CẢNH BÁO: TẤT CẢ {len(accounts)} TÀI KHOẢN ĐỀU ĐÃ HẾT TÍN DỤNG! Còn {len(scene_queue)} cảnh đang chờ. Hãy thêm Tài khoản mới + Proxy IP rồi bấm Chạy tiếp!"
            )

        add_log(f"🏁 Đã kết thúc lượt tạo cảnh 8s cho Dự án [{proj['name']}]!")

        # Tự động kích hoạt Hậu Kỳ (Ghép cảnh + Nhạc nền BGM + Sound Effects SFX + Bắn lệnh Antigravity 2.0)
        if auto_postprocess:
            try:
                add_log("🤖 [Auto Post-Production] Tự động kích hoạt Hậu Kỳ FFmpeg + Antigravity 2.0 Hook...")
                run_full_post_production(
                    project_id=project_id,
                    bgm_filename=bgm_filename,
                    enable_sfx=enable_sfx,
                    trigger_antigravity_agent=True,
                )
            except Exception as post_err:
                add_log(f"⚠️ Lỗi khi chạy Hậu Kỳ tự động: {post_err}")

        WORKER_STATE["running"] = False
        WORKER_STATE["project_id"] = None

    threading.Thread(target=_run_batch_job, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/projects/<int:project_id>/merge", methods=["POST"])
def api_merge_project(project_id: int):
    data = request.json or {}
    bgm_filename = data.get("bgm_filename", "auto")
    enable_sfx = bool(data.get("enable_sfx", True))
    bgm_volume = float(data.get("bgm_volume", 0.28))
    sfx_volume = float(data.get("sfx_volume", 0.65))
    try:
        merged_path = run_full_post_production(
            project_id=project_id,
            bgm_filename=bgm_filename,
            enable_sfx=enable_sfx,
            bgm_volume=bgm_volume,
            sfx_volume=sfx_volume,
            trigger_antigravity_agent=True,
        )
        return jsonify({"ok": True, "merged_video_path": merged_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/audio-library", methods=["GET"])
def api_get_audio_library():
    return jsonify({"ok": True, "library": list_audio_library(), "bgm_dir": BGM_DIR, "sfx_dir": SFX_DIR})


@app.route("/api/audio-library/upload", methods=["POST"])
def api_upload_audio_library():
    kind = request.form.get("kind", "bgm")
    f = request.files.get("audio_file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Chưa chọn file âm thanh (.mp3 / .wav)!"}), 400
    safe_name = secure_filename(f.filename)
    target_dir = BGM_DIR if kind == "bgm" else SFX_DIR
    out_p = os.path.join(target_dir, safe_name)
    f.save(out_p)
    add_log(f"🎵 Đã thêm file âm thanh [{safe_name}] vào thư viện {kind.upper()} ({target_dir})")
    return jsonify({"ok": True, "filename": safe_name, "library": list_audio_library()})


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    return jsonify({"ok": True, "logs": LIVE_LOGS[-120:], "worker": WORKER_STATE})


@app.route("/media/uploads/<path:filename>")
def serve_uploads(filename: str):
    return send_from_directory(UPLOADS_DIR, filename)


@app.route("/media/outputs/<path:filename>")
def serve_outputs(filename: str):
    return send_from_directory(OUTPUTS_DIR, filename)


# ==============================================================================
# WEB DASHBOARD UI (SINGLE-PAGE MODERN APP)
# ==============================================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Flow AI Video Studio — Multi-Account & Multi-IP 8s Scene Automation</title>
<style>
:root {
    --bg: #0f172a;
    --card: #1e293b;
    --card-border: #334155;
    --primary: #10b981;
    --primary-hover: #059669;
    --accent: #f59e0b;
    --danger: #ef4444;
    --text: #f8fafc;
    --muted: #94a3b8;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 20px;
}
.container { max-width: 1380px; margin: 0 auto; }
.header {
    display: flex; justify-content: space-between; align-items: center;
    background: linear-gradient(135deg, #064e3b 0%, #1e293b 100%);
    border: 1px solid #10b98155;
    padding: 20px 28px; border-radius: 16px; margin-bottom: 24px;
}
.header h1 { font-size: 1.55rem; font-weight: 800; color: #34d399; }
.header p { color: #cbd5e1; font-size: 0.92rem; margin-top: 4px; }
.badge {
    display: inline-block; padding: 5px 12px; border-radius: 999px;
    font-size: 0.8rem; font-weight: 700; background: #065f46; color: #6ee7b7;
    border: 1px solid #10b981;
}
.grid-2 { display: grid; grid-template-columns: 420px 1fr; gap: 22px; }
.card {
    background: var(--card); border: 1px solid var(--card-border);
    border-radius: 14px; padding: 20px; margin-bottom: 20px;
}
.card h2 {
    font-size: 1.15rem; color: #38bdf8; margin-bottom: 14px;
    display: flex; align-items: center; justify-content: space-between;
}
label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 5px; font-weight: 600; }
input, select, textarea {
    width: 100%; padding: 10px 12px; border-radius: 8px;
    border: 1px solid #475569; background: #0f172a; color: #fff;
    font-size: 0.92rem; margin-bottom: 12px;
}
textarea { min-height: 76px; resize: vertical; }
.btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    padding: 10px 16px; border-radius: 8px; border: none; cursor: pointer;
    font-weight: 700; font-size: 0.9rem; color: #fff; background: var(--primary);
    transition: all 0.2s;
}
.btn:hover { background: var(--primary-hover); transform: translateY(-1px); }
.btn-sm { padding: 6px 11px; font-size: 0.8rem; border-radius: 6px; }
.btn-blue { background: #2563eb; }
.btn-blue:hover { background: #1d4ed8; }
.btn-amber { background: #d97706; }
.btn-amber:hover { background: #b45309; }
.btn-danger { background: var(--danger); }
.acc-item {
    background: #0f172a; border: 1px solid #334155; border-radius: 10px;
    padding: 12px; margin-bottom: 10px;
}
.acc-item__top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.scene-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-top: 12px; }
.scene-table th, .scene-table td {
    border: 1px solid #334155; padding: 10px; text-align: left; vertical-align: top;
}
.scene-table th { background: #0f172a; color: #94a3b8; }
.status-pill {
    display: inline-block; padding: 3px 9px; border-radius: 99px; font-size: 0.75rem; font-weight: 700;
}
.st-pending { background: #334155; color: #e2e8f0; }
.st-running { background: #1e40af; color: #93c5fd; }
.st-completed { background: #065f46; color: #6ee7b7; }
.st-failed { background: #7f1d1d; color: #fca5a5; }
.log-box {
    background: #020617; border: 1px solid #1e293b; border-radius: 10px;
    padding: 14px; font-family: Consolas, monospace; font-size: 0.83rem;
    color: #34d399; height: 240px; overflow-y: auto; white-space: pre-wrap;
}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>🎬 FLOW AI VIDEO STUDIO (VEO 3.1 / OMNI FLASH)</h1>
            <p>Tự động hóa tạo Video 8s/cảnh • Mỗi Tài khoản 1 Proxy IP độc lập • Ảnh/Video tham chiếu + Mô tả tham chiếu + Prompt</p>
        </div>
        <div>
            <span class="badge" id="workerBadge">⚡ Hệ thống Sẵn sàng</span>
        </div>
    </div>

    <div class="grid-2">
        <!-- CỘT TRÁI: QUẢN LÝ TÀI KHOẢN & PROXY IP RIÊNG -->
        <div>
            <div class="card">
                <h2>🛡️ 1. Quản Lý Tài Khoản & Proxy (1 Acc = 1 IP)</h2>
                <label>Tên gợi nhớ tài khoản</label>
                <input type="text" id="accName" placeholder="VD: Acc Flow 01 - US">
                <label>Email Google (Tùy chọn)</label>
                <input type="text" id="accEmail" placeholder="example@gmail.com">
                <label>Proxy IP riêng (http://user:pass@ip:port hoặc ip:port:user:pass)</label>
                <input type="text" id="accProxy" placeholder="http://user:pass@103.11.22.33:8080">
                <label>Link Project Google Flow (tự lưu sau khi đăng nhập)</label>
                <input type="text" id="accFlowUrl" value="https://flow.google.com/">
                <button class="btn" style="width:100%" onclick="addAccount()">➕ Thêm Tài Khoản & Tạo Profile Riêng</button>

                <div id="accList" style="margin-top: 16px;"></div>
            </div>

            <div class="card">
                <h2>📁 2. Tạo Dự Án & Đồng Bộ "Cài Đặt Tác Nhân"</h2>
                <form id="projForm" onsubmit="createProject(event)">
                    <label>Tên Dự Án Video</label>
                    <input type="text" name="name" placeholder="VD: Video Quảng Cáo Xoài Sấy Dẻo NaFarm" required>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
                        <div>
                            <label>Khung hình Video (16:9 / 9:16)</label>
                            <select name="aspect_ratio">
                                <option value="portrait">📱 9:16 Dọc (TikTok/Reels)</option>
                                <option value="landscape">🖥️ 16:9 Ngang (YouTube)</option>
                            </select>
                        </div>
                        <div>
                            <label>Model Video Mặc Định</label>
                            <select name="model_name">
                                <option value="Omni 1.1 Flash" selected>Omni 1.1 Flash (Chuẩn Flow mới)</option>
                                <option value="Veo 3.1 - Fast">Veo 3.1 - Fast (8s)</option>
                                <option value="Veo 3.1 - Quality">Veo 3.1 - Quality (8s)</option>
                            </select>
                        </div>
                    </div>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
                        <div>
                            <label>Số lượng Video / Cảnh</label>
                            <select name="video_count">
                                <option value="x1" selected>x1 (Tiết kiệm Tín dụng)</option>
                                <option value="x2">x2</option>
                                <option value="x3">x3</option>
                                <option value="x4">x4</option>
                            </select>
                        </div>
                        <div>
                            <label>Xác nhận trước khi tạo</label>
                            <select name="confirm_mode">
                                <option value="never" selected>⚡ Không bao giờ (Tự tạo ngay)</option>
                                <option value="always">✋ Luôn luôn</option>
                            </select>
                        </div>
                    </div>
                    <label>Chế độ sử dụng Ảnh/Video tham chiếu</label>
                    <select name="gen_mode">
                        <option value="references">Ingredients / References (R2V - Đồng nhất nhân vật/sản phẩm)</option>
                        <option value="frames">Frames (I2V - Dùng ảnh/video mẫu làm Frame đầu 8s)</option>
                    </select>
                    <label>Ảnh / Video Tham Chiếu Chung (Tùy chọn - .png, .jpg, .webp, .mp4)</label>
                    <input type="file" name="global_ref_media" accept="image/*,video/*">
                    <label>Mô tả Ảnh / Video Tham Chiếu Chung (Character / Product Bible)</label>
                    <textarea name="global_ref_desc" placeholder="VD: Gói xoài sấy dẻo NaFarm túi zip bạc mặt trong suốt, miếng xoài vàng ươm dẻo mềm..."></textarea>
                    <button type="submit" class="btn btn-blue" style="width:100%">✨ Tạo Dự Án Mới</button>
                </form>
            </div>

            <div class="card">
                <h2>🎵 3. Thư Viện Nhạc Nền (BGM) & Hiệu Ứng (SFX)</h2>
                <p style="font-size:0.82rem; color:#94a3b8; margin-bottom:10px;">
                    Lưu sẵn tại <code>D:\Flow-AI-Video-Studio\audio_library\{bgm,sfx}</code>. Khi tạo xong các cảnh 8s, hệ thống tự động nối video + lồng BGM + chèn SFX chuyển cảnh & bắn lệnh sang Antigravity 2.0.
                </p>
                <form onsubmit="uploadAudioLib(event)" style="display:flex; gap:8px; align-items:center; margin-bottom:10px;">
                    <select name="kind" style="width:130px; margin:0;">
                        <option value="bgm">🎵 Nhạc Nền</option>
                        <option value="sfx">🔊 Sound FX</option>
                    </select>
                    <input type="file" name="audio_file" accept=".mp3,.wav,.m4a,.ogg" style="margin:0; flex:1;" required>
                    <button type="submit" class="btn btn-sm">➕ Nạp</button>
                </form>
                <div id="audioLibSummary" style="font-size:0.8rem; color:#cbd5e1; background:#0f172a; padding:10px; border-radius:8px; border:1px solid #334155;"></div>
            </div>
        </div>

        <!-- CỘT PHẢI: DANH SÁCH CẢNH 8S, CHẠY ĐA LUỒNG & NHẬT KÝ -->
        <div>
            <div class="card">
                <h2>
                    <span>🎞️ 4. Kịch Bản Các Cảnh (Mỗi Cảnh = 8 Giây) & Hậu Kỳ Tự Động</span>
                    <select id="projectSelect" style="width: 340px; margin-bottom:0;" onchange="renderSelectedProject()"></select>
                </h2>

                <div id="projectDetailBox">
                    <!-- Thanh chỉnh nhanh 'Cài Đặt Tác Nhân' cho Dự án đang chọn (Setup giống hệt cột bên phải của Google Flow) -->
                    <div id="projAgentSettingsBar" style="background:#0f172a; border:1px solid #38bdf866; padding:12px 15px; border-radius:10px; margin-bottom:14px;"></div>

                    <!-- Form thêm 1 cảnh 8s có Ảnh/Video tham chiếu + Mô tả + Prompt -->
                    <form id="sceneForm" onsubmit="addScene(event)" style="background:#0f172a; padding:15px; border-radius:10px; border:1px solid #334155; margin-bottom:14px;">
                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
                            <div>
                                <label>📎 Ảnh hoặc Video tham chiếu riêng cho cảnh này (.png/.jpg/.mp4)</label>
                                <input type="file" name="ref_media" accept="image/*,video/*">
                                <label>📝 Mô tả Ảnh / Video tham chiếu của cảnh này</label>
                                <textarea name="ref_description" placeholder="VD: Cô gái mặc áo dài truyền thống cầm hộp mứt Tết NaFarm trên bàn gỗ..."></textarea>
                            </div>
                            <div>
                                <label>🎬 Prompt chuyển động của Cảnh (Cố định 8s/cảnh)</label>
                                <textarea name="scene_prompt" style="min-height:112px;" placeholder="VD: Máy quay dolly-in chậm rãi, cô gái mở hộp mứt Tết, ánh nắng ban mai chiếu qua cửa sổ, chuyển động điện ảnh..." required></textarea>
                                <div style="display:flex; gap:10px; align-items:center;">
                                    <select name="assigned_account_id" id="sceneAccSelect" style="margin-bottom:0; flex:1;">
                                        <option value="">🔄 Tự động chia đều cho các Acc (Đa IP)</option>
                                    </select>
                                    <button type="submit" class="btn">➕ Thêm Cảnh 8s</button>
                                </div>
                            </div>
                        </div>
                    </form>

                    <!-- Thanh cấu hình Hậu Kỳ Tự Động (BGM + SFX + Antigravity 2.0 Hook) -->
                    <div style="background:#0f172a; border:1px solid #10b98166; padding:12px 15px; border-radius:10px; margin-bottom:12px;">
                        <div style="display:flex; flex-wrap:wrap; gap:12px; align-items:center; justify-content:space-between;">
                            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                                <label style="margin:0; color:#34d399;">🎵 Nhạc nền (BGM):</label>
                                <select id="bgmSelect" style="width:230px; margin:0;">
                                    <option value="auto">🤖 Tự động chọn theo chủ đề</option>
                                </select>
                                <label style="margin:0; display:flex; align-items:center; gap:5px; cursor:pointer; color:#e2e8f0;">
                                    <input type="checkbox" id="enableSfxToggle" checked style="width:auto; margin:0;">
                                    🔊 Tự gắn Sound Effects (SFX chuyển cảnh 8s & hiệu ứng)
                                </label>
                                <label style="margin:0; display:flex; align-items:center; gap:5px; cursor:pointer; color:#38bdf8;">
                                    <input type="checkbox" id="autoPostToggle" checked style="width:auto; margin:0;">
                                    ⚡ Tự bắn lệnh Hậu Kỳ Antigravity 2.0 ngay khi xong
                                </label>
                                <label style="margin:0; display:flex; align-items:center; gap:5px; cursor:pointer; color:#fbbf24;">
                                    <input type="checkbox" id="headlessToggle" checked style="width:auto; margin:0;">
                                    👻 Chạy ngầm (Headless) khi tạo cảnh 8s (sau khi đã lưu Cookie)
                                </label>
                            </div>
                        </div>
                    </div>

                    <!-- Thanh công cụ Chạy Đa Luồng & Ghép FFmpeg -->
                    <div style="display:flex; gap:10px; align-items:center; justify-content:space-between; margin-bottom:10px;">
                        <div style="display:flex; gap:10px; align-items:center;">
                            <button class="btn" onclick="runCurrentProject()">🚀 CHẠY TỰ ĐỘNG TẤT CẢ CẢNH 8S (ĐA ACC / ĐA IP)</button>
                            <button class="btn btn-amber" onclick="mergeCurrentProject()">🎬 XỬ LÝ HẬU KỲ NGAY (NỐI VIDEO + BGM + SFX + ANTIGRAVITY 2.0)</button>
                        </div>
                        <div style="display:flex; align-items:center; gap:8px;">
                            <label style="margin:0;">Số luồng IP chạy song song:</label>
                            <select id="parallelWorkers" style="width:75px; margin:0;">
                                <option value="1">1</option>
                                <option value="2" selected>2</option>
                                <option value="3">3</option>
                                <option value="4">4</option>
                            </select>
                        </div>
                    </div>

                    <div id="mergedVideoBox" style="margin-bottom:12px;"></div>
                    <div id="scenesTableContainer"></div>
                </div>
            </div>

            <div class="card">
                <h2>📟 Nhật Ký Hoạt Động Trực Tiếp (Live Logs)</h2>
                <div class="log-box" id="logBox">Đang tải nhật ký...</div>
            </div>
        </div>
    </div>
</div>

<script>
let PROJECTS = [];
let ACCOUNTS = [];
let CURRENT_PROJECT_ID = null;

async function loadAll() {
    const [accRes, projRes, libRes] = await Promise.all([
        fetch('/api/accounts').then(r => r.json()),
        fetch('/api/projects').then(r => r.json()),
        fetch('/api/audio-library').then(r => r.json())
    ]);
    if (accRes.ok) {
        ACCOUNTS = accRes.accounts;
        renderAccounts();
    }
    if (projRes.ok) {
        PROJECTS = projRes.projects;
        renderProjectSelector();
    }
    if (libRes.ok && libRes.library) {
        renderAudioLibrary(libRes.library);
    }
}

function renderAudioLibrary(lib) {
    const bgmSel = document.getElementById('bgmSelect');
    const curVal = bgmSel ? bgmSel.value : 'auto';
    if (bgmSel) {
        bgmSel.innerHTML = '<option value="auto">🤖 Tự động chọn theo chủ đề</option>' +
            (lib.bgm || []).map(f => `<option value="${f}" ${f === curVal ? 'selected' : ''}>🎵 ${f}</option>`).join('');
    }
    const sumBox = document.getElementById('audioLibSummary');
    if (sumBox) {
        sumBox.innerHTML = `
            <div><strong>🎵 Nhạc nền (${(lib.bgm || []).length}):</strong> ${(lib.bgm || []).join(', ') || 'Trống'}</div>
            <div style="margin-top:4px;"><strong>🔊 Sound FX (${(lib.sfx || []).length}):</strong> ${(lib.sfx || []).join(', ') || 'Trống'}</div>
        `;
    }
}

async function uploadAudioLib(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const res = await fetch('/api/audio-library/upload', {method: 'POST', body: fd}).then(r => r.json());
    if (!res.ok) { alert(res.error); return; }
    e.target.reset();
    loadAll();
}

function renderAccounts() {
    const box = document.getElementById('accList');
    const sel = document.getElementById('sceneAccSelect');
    sel.innerHTML = '<option value="">🔄 Tự động chia đều & Tự đổi Acc khi hết Tín dụng (Đa IP)</option>' +
        ACCOUNTS.map(a => `<option value="${a.id}">Acc #${a.id}: ${a.name} (${a.status === 'out_of_credits' ? '🚫 Hết tín dụng' : (a.last_ip || 'Chưa check IP')})</option>`).join('');

    if (!ACCOUNTS.length) {
        box.innerHTML = '<p style="color:#94a3b8; font-size:0.88rem;">Chưa có tài khoản nào. Hãy thêm tài khoản + Proxy IP ở trên.</p>';
        return;
    }
    box.innerHTML = ACCOUNTS.map(a => {
        const isOut = a.status === 'out_of_credits';
        const creditBadge = isOut
            ? `<span class="badge" style="background:#7f1d1d; border-color:#ef4444; color:#fca5a5;">🚫 HẾT TÍN DỤNG (Tự bỏ qua)</span>`
            : `<span class="badge" style="background:#065f46; border-color:#10b981; color:#6ee7b7;">${a.credits_info || '✅ Còn tín dụng'}</span>`;
        return `
        <div class="acc-item" style="${isOut ? 'border-color:#ef444488; opacity:0.85;' : ''}">
            <div class="acc-item__top">
                <strong>#${a.id} • ${a.name}</strong>
                <div style="display:flex; gap:5px; align-items:center;">
                    ${creditBadge}
                    <span class="badge" style="background:#1e3a8a; border-color:#3b82f6; color:#93c5fd;">
                        🌐 IP: ${a.last_ip || 'Chưa kiểm tra'}
                    </span>
                </div>
            </div>
            <div style="font-size:0.81rem; color:#94a3b8; margin-bottom:6px;">
                Proxy: <code>${a.proxy_url || 'Direct (Không dùng Proxy)'}</code>
            </div>
            <div style="display:flex; gap:6px; align-items:center; margin-bottom:8px;">
                <input type="text" id="accUrl_${a.id}" value="${a.flow_project_url || 'https://flow.google.com/'}" style="margin:0; padding:6px 9px; font-size:0.8rem; flex:1;" title="Link Project trực tiếp (VD: https://flow.google.com/project/...)">
                <button class="btn btn-sm" onclick="saveAccUrl(${a.id})" title="Lưu Link Project trực tiếp">💾 Lưu Link</button>
            </div>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button class="btn btn-sm btn-blue" onclick="checkIp(${a.id})">🌐 Check IP</button>
                <button class="btn btn-sm btn-amber" onclick="openLogin(${a.id})">🔐 Mở Chrome Đăng Nhập</button>
                <button class="btn btn-sm" style="background:${isOut ? '#10b981' : '#b91c1c'};" onclick="toggleCreditStatus(${a.id}, '${isOut ? 'logged_in' : 'out_of_credits'}')">
                    ${isOut ? '🔄 Khôi Phục Tín Dụng' : '🚫 Báo Hết Tín Dụng'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteAccount(${a.id})">🗑️</button>
            </div>
        </div>
        `;
    }).join('');
}

async function saveAccUrl(id) {
    const val = document.getElementById(`accUrl_${id}`).value.trim();
    await fetch(`/api/accounts/${id}/update`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({flow_project_url: val})
    });
    loadAll();
}

async function toggleCreditStatus(id, newStatus) {
    const credits_info = newStatus === 'out_of_credits' ? '🚫 Hết tín dụng (0 credit)' : '✅ Còn tín dụng';
    await fetch(`/api/accounts/${id}/update`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: newStatus, credits_info})
    });
    loadAll();
}

async function addAccount() {
    const name = document.getElementById('accName').value.trim();
    const email = document.getElementById('accEmail').value.trim();
    const proxy_url = document.getElementById('accProxy').value.trim();
    const flow_project_url = document.getElementById('accFlowUrl').value.trim();
    const res = await fetch('/api/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, email, proxy_url, flow_project_url})
    }).then(r => r.json());
    if (!res.ok) { alert(res.error); return; }
    document.getElementById('accName').value = '';
    document.getElementById('accProxy').value = '';
    loadAll();
}

async function checkIp(id) {
    const res = await fetch(`/api/accounts/${id}/check-ip`, {method: 'POST'}).then(r => r.json());
    if (!res.ok) alert('Lỗi Proxy: ' + res.error);
    else if (res.conflict) alert(`⚠️ CẢNH BÁO: IP ${res.ip} đang bị trùng với tài khoản [${res.conflict}]!`);
    loadAll();
}

async function openLogin(id) {
    const res = await fetch(`/api/accounts/${id}/login`, {method: 'POST'}).then(r => r.json());
    alert(res.message || 'Đã mở trình duyệt!');
}

async function deleteAccount(id) {
    if (!confirm('Xóa tài khoản này?')) return;
    await fetch(`/api/accounts/${id}`, {method: 'DELETE'});
    loadAll();
}

function renderProjectSelector() {
    const sel = document.getElementById('projectSelect');
    if (!PROJECTS.length) {
        sel.innerHTML = '<option value="">-- Chưa có dự án nào --</option>';
        document.getElementById('scenesTableContainer').innerHTML = '<p style="color:#94a3b8;">Hãy tạo Dự án ở cột bên trái trước.</p>';
        return;
    }
    if (!CURRENT_PROJECT_ID || !PROJECTS.find(p => p.id == CURRENT_PROJECT_ID)) {
        CURRENT_PROJECT_ID = PROJECTS[0].id;
    }
    sel.innerHTML = PROJECTS.map(p =>
        `<option value="${p.id}" ${p.id == CURRENT_PROJECT_ID ? 'selected' : ''}>#${p.id} - ${p.name} (${p.scenes.length} cảnh 8s)</option>`
    ).join('');
    renderSelectedProject();
}

function renderSelectedProject() {
    const sel = document.getElementById('projectSelect');
    CURRENT_PROJECT_ID = sel.value;
    const proj = PROJECTS.find(p => p.id == CURRENT_PROJECT_ID);
    const box = document.getElementById('scenesTableContainer');
    const mergedBox = document.getElementById('mergedVideoBox');
    const agentBar = document.getElementById('projAgentSettingsBar');
    if (!proj) return;

    if (agentBar) {
        const curRatio = proj.aspect_ratio || 'portrait';
        const curModel = proj.model_name || 'Omni 1.1 Flash';
        const curCount = proj.video_count || 'x1';
        const curConfirm = proj.confirm_mode || 'never';
        const curGenMode = proj.gen_mode || 'references';
        agentBar.innerHTML = `
            <div style="font-size:0.85rem; font-weight:700; color:#38bdf8; margin-bottom:8px;">
                ⚙️ Đồng bộ "Cài Đặt Tác Nhân" (Trước khi tạo Video, Tool sẽ tự động bật và bấm [Lưu] trên Google Flow đúng như dưới đây):
            </div>
            <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
                <div>
                    <label style="font-size:0.76rem; margin-bottom:2px;">Khung hình Video:</label>
                    <select id="editProjAspect" style="width:165px; margin:0; padding:6px 8px;">
                        <option value="portrait" ${curRatio === 'portrait' ? 'selected' : ''}>📱 9:16 (Khung Dọc)</option>
                        <option value="landscape" ${curRatio === 'landscape' ? 'selected' : ''}>🖥️ 16:9 (Khung Ngang)</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:0.76rem; margin-bottom:2px;">Số lượng Video:</label>
                    <select id="editProjCount" style="width:90px; margin:0; padding:6px 8px;">
                        <option value="x1" ${curCount === 'x1' ? 'selected' : ''}>x1</option>
                        <option value="x2" ${curCount === 'x2' ? 'selected' : ''}>x2</option>
                        <option value="x3" ${curCount === 'x3' ? 'selected' : ''}>x3</option>
                        <option value="x4" ${curCount === 'x4' ? 'selected' : ''}>x4</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:0.76rem; margin-bottom:2px;">Model Tạo Video:</label>
                    <select id="editProjModel" style="width:180px; margin:0; padding:6px 8px;">
                        <option value="Omni 1.1 Flash" ${curModel.includes('Omni') ? 'selected' : ''}>Omni 1.1 Flash</option>
                        <option value="Veo 3.1 - Fast" ${curModel.includes('Fast') ? 'selected' : ''}>Veo 3.1 - Fast</option>
                        <option value="Veo 3.1 - Quality" ${curModel.includes('Quality') ? 'selected' : ''}>Veo 3.1 - Quality</option>
                    </select>
                </div>
                <div>
                    <label style="font-size:0.76rem; margin-bottom:2px;">Xác nhận trước khi tạo:</label>
                    <select id="editProjConfirm" style="width:190px; margin:0; padding:6px 8px;">
                        <option value="never" ${curConfirm === 'never' ? 'selected' : ''}>⚡ Không bao giờ (Tự tạo)</option>
                        <option value="always" ${curConfirm === 'always' ? 'selected' : ''}>✋ Luôn luôn</option>
                    </select>
                </div>
                <div style="padding-top:16px;">
                    <button class="btn btn-sm btn-blue" onclick="saveProjectAgentSettings(${proj.id})">💾 Lưu Cài Đặt Tác Nhân</button>
                </div>
            </div>
        `;
    }

    if (proj.merged_video_path) {
        const rel = proj.merged_video_path.split('outputs')[1].replace(/^[\\\\/]/, '').replace(/\\\\/g, '/');
        mergedBox.innerHTML = `
            <div style="background:#064e3b; border:1px solid #10b981; padding:12px; border-radius:10px; display:flex; justify-content:space-between; align-items:center;">
                <div>
                    <strong>🎉 Video Ghép Hoàn Chỉnh (${proj.scenes.length * 8}s):</strong>
                    <code>${proj.merged_video_path}</code>
                </div>
                <a class="btn btn-sm" href="/media/outputs/${rel}" target="_blank">▶️ Xem / Tải Video Tổng</a>
            </div>
        `;
    } else {
        mergedBox.innerHTML = '';
    }

    if (!proj.scenes.length) {
        box.innerHTML = '<p style="color:#94a3b8;">Dự án chưa có cảnh nào. Hãy thêm Cảnh 8s (kèm ảnh/video tham chiếu + mô tả + prompt) ở khung trên!</p>';
        return;
    }

    box.innerHTML = `
        <table class="scene-table">
            <thead>
                <tr>
                    <th style="width:65px;">Cảnh</th>
                    <th style="width:190px;">Tham chiếu & Mô tả</th>
                    <th>Prompt Cảnh (8s) & Prompt Tổng Hợp</th>
                    <th style="width:150px;">Acc / IP Chạy</th>
                    <th style="width:130px;">Trạng thái</th>
                    <th style="width:130px;">Video 8s</th>
                </tr>
            </thead>
            <tbody>
                ${proj.scenes.map(s => {
                    const refFile = s.ref_media_path || proj.global_ref_media || '';
                    const refName = refFile ? refFile.split(/[\\\\/]/).pop() : 'Không có file';
                    let vidHtml = '—';
                    if (s.video_path) {
                        const vRel = s.video_path.split('outputs')[1].replace(/^[\\\\/]/, '').replace(/\\\\/g, '/');
                        vidHtml = `<video src="/media/outputs/${vRel}" controls style="width:120px; border-radius:6px;"></video>`;
                    }
                    return `
                        <tr>
                            <td><strong>#${s.scene_order}</strong><br><span class="badge">8s</span></td>
                            <td>
                                <div style="font-size:0.78rem; color:#38bdf8;">📎 ${refName}</div>
                                <div style="font-size:0.8rem; color:#cbd5e1; margin-top:4px;">${s.ref_description || proj.global_ref_desc || '—'}</div>
                            </td>
                            <td>
                                <div style="font-weight:600; color:#fff;">${s.scene_prompt}</div>
                                <div style="font-size:0.76rem; color:#94a3b8; margin-top:4px;"><em>Composed:</em> ${s.composed_prompt}</div>
                                ${s.error_msg ? `<div style="color:#f87171; font-size:0.78rem; margin-top:4px;">❌ ${s.error_msg}</div>` : ''}
                            </td>
                            <td>
                                <div>Acc ID: ${s.assigned_account_id || 'Auto'}</div>
                                <div style="font-size:0.78rem; color:#6ee7b7;">IP: ${s.account_ip_used || '—'}</div>
                            </td>
                            <td><span class="status-pill st-${s.status}">${s.status.toUpperCase()}</span></td>
                            <td>
                                ${vidHtml}
                                <div style="margin-top:6px;">
                                    <button class="btn btn-sm btn-danger" onclick="deleteScene(${s.id})">Xóa</button>
                                </div>
                            </td>
                        </tr>
                    `;
                }).join('')}
            </tbody>
        </table>
    `;
}

async function createProject(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const res = await fetch('/api/projects', {method: 'POST', body: fd}).then(r => r.json());
    if (!res.ok) { alert(res.error); return; }
    CURRENT_PROJECT_ID = res.project_id;
    e.target.reset();
    loadAll();
}

async function saveProjectAgentSettings(projId) {
    const aspect_ratio = document.getElementById('editProjAspect').value;
    const video_count = document.getElementById('editProjCount').value;
    const model_name = document.getElementById('editProjModel').value;
    const confirm_mode = document.getElementById('editProjConfirm').value;
    const res = await fetch(`/api/projects/${projId}/settings`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({aspect_ratio, video_count, model_name, confirm_mode})
    }).then(r => r.json());
    if (!res.ok) alert(res.error);
    else alert('✅ Đã lưu Cài Đặt Tác Nhân cho Dự án! Trước khi tạo video, Tool sẽ tự động chỉnh và bấm [Lưu] trên Flow.');
    loadAll();
}

async function addScene(e) {
    e.preventDefault();
    if (!CURRENT_PROJECT_ID) { alert('Hãy tạo dự án trước!'); return; }
    const fd = new FormData(e.target);
    const res = await fetch(`/api/projects/${CURRENT_PROJECT_ID}/scenes`, {method: 'POST', body: fd}).then(r => r.json());
    if (!res.ok) { alert(res.error); return; }
    e.target.reset();
    loadAll();
}

async function deleteScene(id) {
    await fetch(`/api/scenes/${id}`, {method: 'DELETE'});
    loadAll();
}

async function runCurrentProject() {
    if (!CURRENT_PROJECT_ID) return;
    const parallel_workers = parseInt(document.getElementById('parallelWorkers').value || '2', 10);
    const bgm_filename = document.getElementById('bgmSelect') ? document.getElementById('bgmSelect').value : 'auto';
    const enable_sfx = document.getElementById('enableSfxToggle') ? document.getElementById('enableSfxToggle').checked : true;
    const auto_postprocess = document.getElementById('autoPostToggle') ? document.getElementById('autoPostToggle').checked : true;
    const headless_mode = document.getElementById('headlessToggle') ? document.getElementById('headlessToggle').checked : true;
    const res = await fetch(`/api/projects/${CURRENT_PROJECT_ID}/run`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({parallel_workers, bgm_filename, enable_sfx, auto_postprocess, headless_mode})
    }).then(r => r.json());
    if (!res.ok) alert(res.error);
    loadAll();
}

async function mergeCurrentProject() {
    if (!CURRENT_PROJECT_ID) return;
    const bgm_filename = document.getElementById('bgmSelect') ? document.getElementById('bgmSelect').value : 'auto';
    const enable_sfx = document.getElementById('enableSfxToggle') ? document.getElementById('enableSfxToggle').checked : true;
    const res = await fetch(`/api/projects/${CURRENT_PROJECT_ID}/merge`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({bgm_filename, enable_sfx})
    }).then(r => r.json());
    if (!res.ok) alert(res.error);
    else alert('🎉 Đã xử lý hậu kỳ thành công (Ghép cảnh + BGM + SFX + Antigravity 2.0): ' + res.merged_video_path);
    loadAll();
}

async function pollLogs() {
    try {
        const res = await fetch('/api/logs').then(r => r.json());
        if (res.ok) {
            const box = document.getElementById('logBox');
            box.textContent = res.logs.join('\n') || 'Chưa có nhật ký...';
            box.scrollTop = box.scrollHeight;
            const badge = document.getElementById('workerBadge');
            if (res.worker && res.worker.running) {
                badge.textContent = '🔥 Đang chạy tạo Video 8s...';
                badge.style.background = '#991b1b';
                loadAll();
            } else {
                badge.textContent = '⚡ Hệ thống Sẵn sàng';
                badge.style.background = '#065f46';
            }
        }
    } catch (e) {}
}

loadAll();
setInterval(pollLogs, 2000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return DASHBOARD_HTML


if __name__ == "__main__":
    try:
        from desktop_escape import get_current_desktop_name, spawn_on_default_desktop
        import sys
        if os.name == "nt" and get_current_desktop_name().lower() != "default":
            cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            spawn_on_default_desktop(cmd, work_dir=os.path.dirname(os.path.abspath(__file__)), show_console=False)
            sys.exit(0)
    except Exception:
        pass
    add_log("✨ Khởi động Flow AI Video Studio tại địa chỉ: http://127.0.0.1:8686")
    app.run(host="127.0.0.1", port=8686, debug=False, threaded=True)
