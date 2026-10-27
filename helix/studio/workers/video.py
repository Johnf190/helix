"""Video providers — quote/confirm pipeline (money can NEVER move without an
explicit /confirm; the db layer enforces that, this module just prices & runs).

README for the user (live activation, deliberately NOT done in this plan):
  1. Pick a model and put your fal.ai key in the environment as FAL_KEY
     (e.g. in start_studio.sh: export FAL_KEY=...).
  2. VERIFY PRICES at https://fal.ai/models/<model_id> — the PRICES table
     below is a static snapshot, candidates researched as of 2026-09-05:
       fal-ai/kling-video/v2.1/standard   ~$0.05/s  (5s clip ≈ $0.25)
       fal-ai/minimax/hailuo-02/standard  ~$0.045/s (6s clip ≈ $0.27)
       fal-ai/veo3/fast                   ~$0.40/s  (premium, audio)
       fal-ai/ltx-video-13b-098-distilled ~$0.02/s  (cheapest, lower quality)
     These numbers are NOT billing-grade until re-checked against the fal
     pricing page on activation day. quote() refuses unknown models rather
     than guessing.
  3. The queue API contract: POST https://queue.fal.run/<model_id> with
     header "Authorization: Key $FAL_KEY" returns {request_id, status_url,
     response_url}; poll status_url until status == "COMPLETED"; GET
     response_url -> {video: {url: ...}} (shape varies slightly per model).
"""
import json
import os
import time


class VideoProvider:
    def quote(self, payload) -> float:
        raise NotImplementedError

    def run(self, job):
        """-> (outputs: list[str], cost_usd: float)"""
        raise NotImplementedError


def _job_params(job):
    params = job.get("params") or "{}"
    if isinstance(params, str):
        params = json.loads(params)
    return params


def _out_path(out_root, job):
    project = job.get("project_name") or "default"
    dest_dir = os.path.join(out_root, project)
    os.makedirs(dest_dir, exist_ok=True)
    name = "job%d_0.mp4" % job["id"]
    return os.path.join(dest_dir, name), "/static/studio/%s/%s" % (project,
                                                                   name)


class MockProvider(VideoProvider):
    """Tests only: fixed quote, writes a stub .mp4."""
    def __init__(self, out_root):
        self.out_root = out_root

    def quote(self, payload):
        return 0.42

    def run(self, job):
        disk, web = _out_path(self.out_root, job)
        with open(disk, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42stub video for tests")
        return [web], 0.42


class FalProvider(VideoProvider):
    # USD per second of output video. Static snapshot dated 2026-09-05 from
    # fal.ai model pages — RE-VERIFY before live use (see module docstring).
    PRICES = {
        "fal-ai/kling-video/v2.1/standard": 0.05,
        "fal-ai/minimax/hailuo-02/standard": 0.045,
        "fal-ai/veo3/fast": 0.40,
        "fal-ai/ltx-video-13b-098-distilled": 0.02,
    }
    DEFAULT_SECONDS = 5
    QUEUE = "https://queue.fal.run"

    def __init__(self, out_root):
        self.out_root = out_root
        self.key = os.environ.get("FAL_KEY")

    def quote(self, payload):
        model = payload.get("model")
        if model not in self.PRICES:
            raise ValueError(
                "unknown video model %r (known: %s)"
                % (model, ", ".join(sorted(self.PRICES))))
        params = payload.get("params") or {}
        seconds = float(params.get("seconds", self.DEFAULT_SECONDS))
        return round(self.PRICES[model] * seconds, 4)

    def run(self, job):
        if not self.key:
            raise RuntimeError("FAL_KEY not set")
        import requests
        model = job.get("model")
        params = _job_params(job)
        headers = {"Authorization": "Key %s" % self.key}
        body = {"prompt": job.get("prompt") or ""}
        if params.get("seconds"):
            body["duration"] = params["seconds"]
        r = requests.post("%s/%s" % (self.QUEUE, model), json=body,
                          headers=headers, timeout=60)
        r.raise_for_status()
        sub = r.json()
        status_url = sub["status_url"]
        response_url = sub["response_url"]
        deadline = time.time() + 1800
        while time.time() < deadline:
            s = requests.get(status_url, headers=headers, timeout=30).json()
            if s.get("status") == "COMPLETED":
                break
            if s.get("status") in ("FAILED", "CANCELLED"):
                raise RuntimeError("fal job %s: %s" % (s.get("status"), s))
            time.sleep(5)
        else:
            raise RuntimeError("fal job timed out after 30 min")
        result = requests.get(response_url, headers=headers, timeout=60).json()
        video = result.get("video") or {}
        url = video.get("url") if isinstance(video, dict) else None
        if not url:
            raise RuntimeError("no video url in fal response: %s" % result)
        disk, web = _out_path(self.out_root, job)
        with requests.get(url, stream=True, timeout=300) as dl:
            dl.raise_for_status()
            with open(disk, "wb") as f:
                for chunk in dl.iter_content(1 << 20):
                    f.write(chunk)
        # fal bills what was quoted for these fixed-length models; record the
        # quote as the cost (no billing API to read actuals from, 2026-09-05)
        cost = job.get("price_quote_usd")
        return [web], cost

    # NOTE: quote() must never talk to the network — it runs inside POST
    # /api/job and the price shown at confirm time has to be instant.


def get_provider(out_root=None):
    """FalProvider if FAL_KEY is set, else None (API answers 503)."""
    if not os.environ.get("FAL_KEY"):
        return None
    return FalProvider(out_root)
