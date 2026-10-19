import os, re, time, glob, shutil, socket, ipaddress, tempfile, threading, logging
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file
import yt_dlp

ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://sourav-garai.vercel.app,https://souravgarai.is-a.dev,http://localhost:8000").split(",") if o.strip()}
MAX_BYTES = 300 * 1024 * 1024
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
sem = threading.Semaphore(2)
hits = {}
lock = threading.Lock()


@app.after_request
def cors(resp):
    o = request.headers.get("Origin", "")
    if o in ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = o
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
    return resp


def limited(kind, limit, window):
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    now = time.time()
    with lock:
        arr = [t for t in hits.get((kind, ip), []) if now - t < window]
        if len(arr) >= limit:
            hits[(kind, ip)] = arr
            return True
        arr.append(now)
        hits[(kind, ip)] = arr
    return False


def safe_url(u):
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def friendly(e):
    m = str(e).lower()
    if "sign in to confirm" in m or "not a bot" in m or "confirm you" in m:
        return "blocked", "This site blocked the server, which happens a lot with YouTube on free hosting. Try another link."
    if "private" in m or "login" in m or "log in" in m or "members-only" in m:
        return "private", "This video is private or needs a login, so it cannot be downloaded."
    if "unsupported url" in m:
        return "unsupported", "That link is not a video page this tool can read."
    if "larger than" in m or "max-filesize" in m or "file is larger" in m:
        return "too_big", "That file is over the 300 MB limit of this free server. Pick a lower quality."
    if "unavailable" in m or "removed" in m or "not available" in m:
        return "unavailable", "This video is not available."
    return "failed", "Could not get this video. The site may block downloads from servers."


def fsize(f, duration):
    n = f.get("filesize") or f.get("filesize_approx")
    if not n and f.get("tbr") and duration:
        n = f["tbr"] * 1000 / 8 * duration
    return int(n) if n else None


def build_options(d):
    """One entry per real height the source offers, with an approximate merged size."""
    dur = d.get("duration")
    fmts = d.get("formats") or []
    audio_sizes = [fsize(f, dur) for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    audio_sizes = [x for x in audio_sizes if x]
    audio = max(audio_sizes) if audio_sizes else None
    best = {}
    for f in fmts:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = fsize(f, dur)
        if size and f.get("acodec") in (None, "none") and audio:
            size += audio
        cur = best.get(h)
        if cur is None or (size or 0) > (cur or 0):
            best[h] = size
    out = []
    for h in sorted(best, reverse=True):
        if h < 144:
            continue
        size = best[h]
        out.append({"height": h, "label": {4320: "8K", 2160: "4K", 1440: "2K"}.get(h), "size": size,
                    "fits": None if size is None else size <= MAX_BYTES})
    return out, audio


@app.route("/")
def home():
    return jsonify(ok=True, service="video-api")


@app.route("/api/ping")
def ping():
    return jsonify(ok=True)


@app.route("/api/info")
def info():
    if limited("info", 20, 600):
        return jsonify(error="rate", message="Too many requests. Wait a few minutes."), 429
    u = (request.args.get("url") or "").strip()
    if not safe_url(u):
        return jsonify(error="bad_url", message="Paste a full http or https video link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20, "js_runtimes": {"node": {}}}) as y:
            d = y.extract_info(u, download=False)
    except Exception as e:
        code, msg = friendly(e)
        return jsonify(error=code, message=msg), 422
    opts, audio = build_options(d)
    heights = [o["height"] for o in opts]
    return jsonify(title=d.get("title"), uploader=d.get("uploader"), duration=d.get("duration"), thumbnail=d.get("thumbnail"),
                   heights=heights, options=opts, audio_size=audio, limit=MAX_BYTES, site=d.get("extractor_key"))


@app.route("/api/download")
def download():
    if limited("dl", 6, 600):
        return jsonify(error="rate", message="Download limit reached. Try again in a few minutes."), 429
    u = (request.args.get("url") or "").strip()
    q = request.args.get("q", "720")
    if not safe_url(u) or not (q in ("audio", "best") or (q.isdigit() and 144 <= int(q) <= 4320)):
        return jsonify(error="bad_request", message="Bad request."), 400
    if not sem.acquire(blocking=False):
        return jsonify(error="busy", message="The server is busy with other downloads. Try again in a minute."), 503
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="dl_")
        o = {"quiet": True, "noplaylist": True, "outtmpl": os.path.join(tmp, "v.%(ext)s"), "max_filesize": MAX_BYTES, "socket_timeout": 20, "restrictfilenames": True}
        if q == "audio":
            o.update(format="bestaudio/best", postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}])
        elif q == "best":
            o.update(format="bv*+ba/b", merge_output_format="mp4")
        else:
            o.update(format=f"bv*[height<={q}]+ba/b[height<={q}]", merge_output_format="mp4")
        with yt_dlp.YoutubeDL(o) as y:
            d = y.extract_info(u, download=True)
        files = [f for f in glob.glob(os.path.join(tmp, "v.*")) if os.path.basename(f).count(".") == 1 and not f.endswith(".part")]
        if not files:
            raise RuntimeError("file is larger than max-filesize")
        path = files[0]
        # Post-processing/merged outputs may exceed yt-dlp's source-file cap.
        if os.path.getsize(path) > MAX_BYTES:
            raise RuntimeError("file is larger than max-filesize")
        title = re.sub(r"[^\w\-. ]+", "", d.get("title") or "video").strip()[:80] or "video"
        name = f"{title}{os.path.splitext(path)[1]}"
        # Construct the response before scheduling cleanup so serving failures
        # take the same cleanup path as extraction failures.
        response = send_file(path, as_attachment=True, download_name=name)
        def later():
            time.sleep(60)
            shutil.rmtree(tmp, ignore_errors=True)
        threading.Thread(target=later, daemon=True).start()
        return response
    except Exception as e:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        code, msg = friendly(e)
        return jsonify(error=code, message=msg), 422
    finally:
        sem.release()

import os, re, time, glob, shutil, socket, ipaddress, tempfile, threading, logging
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file
import yt_dlp

ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://sourav-garai.vercel.app,https://souravgarai.is-a.dev,http://localhost:8000").split(",") if o.strip()}
MAX_BYTES = 300 * 1024 * 1024
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
sem = threading.Semaphore(2)
hits = {}
lock = threading.Lock()


@app.after_request
def cors(resp):
    o = request.headers.get("Origin", "")
    if o in ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = o
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
    return resp


def limited(kind, limit, window):
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    now = time.time()
    with lock:
        arr = [t for t in hits.get((kind, ip), []) if now - t < window]
        if len(arr) >= limit:
            hits[(kind, ip)] = arr
            return True
        arr.append(now)
        hits[(kind, ip)] = arr
    return False


def safe_url(u):
    try:
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def friendly(e):
    m = str(e).lower()
    if "sign in to confirm" in m or "not a bot" in m or "confirm you" in m:
        return "blocked", "This site blocked the server, which happens a lot with YouTube on free hosting. Try another link."
    if "private" in m or "login" in m or "log in" in m or "members-only" in m:
        return "private", "This video is private or needs a login, so it cannot be downloaded."
    if "unsupported url" in m:
        return "unsupported", "That link is not a video page this tool can read."
    if "larger than" in m or "max-filesize" in m or "file is larger" in m:
        return "too_big", "That file is over the 300 MB limit. Try a lower quality."
    if "unavailable" in m or "removed" in m or "not available" in m:
        return "unavailable", "This video is not available."
    return "failed", "Could not get this video. The site may block downloads from servers."


@app.route("/")
def home():
    return jsonify(ok=True, service="video-api")


@app.route("/api/ping")
def ping():
    return jsonify(ok=True)


@app.route("/api/info")
def info():
    if limited("info", 20, 600):
        return jsonify(error="rate", message="Too many requests. Wait a few minutes."), 429
    u = (request.args.get("url") or "").strip()
    if not safe_url(u):
        return jsonify(error="bad_url", message="Paste a full http or https video link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 20}) as y:
            d = y.extract_info(u, download=False)
    except Exception as e:
        code, msg = friendly(e)
        return jsonify(error=code, message=msg), 422
    heights = sorted({f.get("height") for f in d.get("formats", []) if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    opts = [h for h in (1080, 720, 480, 360) if any(x >= h for x in heights)] or ([heights[-1]] if heights else [])
    return jsonify(title=d.get("title"), uploader=d.get("uploader"), duration=d.get("duration"), thumbnail=d.get("thumbnail"), heights=opts, site=d.get("extractor_key"))


@app.route("/api/download")
def download():
    if limited("dl", 6, 600):
        return jsonify(error="rate", message="Download limit reached. Try again in a few minutes."), 429
    u = (request.args.get("url") or "").strip()
    q = request.args.get("q", "720")
    if not safe_url(u) or q not in ("1080", "720", "480", "360", "audio", "best"):
        return jsonify(error="bad_request", message="Bad request."), 400
    if not sem.acquire(blocking=False):
        return jsonify(error="busy", message="The server is busy with other downloads. Try again in a minute."), 503
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix="dl_")
        o = {"quiet": True, "noplaylist": True, "outtmpl": os.path.join(tmp, "v.%(ext)s"), "max_filesize": MAX_BYTES, "socket_timeout": 20, "restrictfilenames": True}
        if q == "audio":
            o.update(format="bestaudio/best", postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}])
        elif q == "best":
            o.update(format="bv*+ba/b", merge_output_format="mp4")
        else:
            o.update(format=f"bv*[height<={q}]+ba/b[height<={q}]/b", merge_output_format="mp4")
        with yt_dlp.YoutubeDL(o) as y:
            d = y.extract_info(u, download=True)
        files = [f for f in glob.glob(os.path.join(tmp, "v.*")) if not f.endswith(".part")]
        if not files:
            raise RuntimeError("file is larger than max-filesize")
        path = files[0]
        # Post-processing/merged outputs may exceed yt-dlp's source-file cap.
        if os.path.getsize(path) > MAX_BYTES:
            raise RuntimeError("file is larger than max-filesize")
        title = re.sub(r"[^\w\-. ]+", "", d.get("title") or "video").strip()[:80] or "video"
        name = f"{title}{os.path.splitext(path)[1]}"
        # Construct the response before scheduling cleanup so serving failures
        # take the same cleanup path as extraction failures.
        response = send_file(path, as_attachment=True, download_name=name)
        def later():
            time.sleep(60)
            shutil.rmtree(tmp, ignore_errors=True)
        threading.Thread(target=later, daemon=True).start()
        return response
    except Exception as e:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        code, msg = friendly(e)
        return jsonify(error=code, message=msg), 422
    finally:
        sem.release()
