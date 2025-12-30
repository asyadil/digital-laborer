"""Guided remediation playbooks for common error categories.

This module maps error codes (from adapters/health/ops) to concise
step-by-step instructions that can be shown to the operator via Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Playbook:
    title: str
    steps: List[str]
    allow_retry: bool = True
    allow_skip: bool = False
    allow_rotate: bool = False
    auto_safe: bool = False  # safe to auto-act (retry/rotate) during auto-mode


_PLAYBOOKS: Dict[str, Playbook] = {
    # Auth / credential issues
    "auth_failed": Playbook(
        title="🔑 Auth gagal",
        steps=[
            "Buka console/OAuth untuk platform ini dan generate token/refresh token baru.",
            "Kirim di Telegram: `/secret NAMA_TOKEN=isi_token_baru` (contoh: `/secret YOUTUBE_REFRESH_TOKEN=abc`).",
            "Jika perlu client_secret: `/secret YOUTUBE_CLIENT_SECRET=...`.",
            "Tekan tombol Retry.",
        ],
        allow_retry=True,
    ),
    "token_expired": Playbook(
        title="⏳ Token kedaluwarsa",
        steps=[
            "Regenerasi token/refresh token di console platform.",
            "Set nilai baru via `/secret NAMA_TOKEN=...`.",
            "Tekan Retry.",
        ],
        allow_retry=True,
    ),
    "missing_secret": Playbook(
        title="🔒 Secret hilang",
        steps=[
            "Ambil nilai (client_id/client_secret/token) dari console.",
            "Kirim `/secret NAMA=VALUE` di Telegram (contoh: `/secret REDDIT_CLIENT_ID=...`).",
            "Tekan Retry.",
        ],
        allow_retry=True,
        allow_skip=False,
    ),
    # Rate limit / quota
    "rate_limit": Playbook(
        title="🚦 Rate limit",
        steps=[
            "Tunggu sesuai retry-after/backoff (minimal 5-15 menit).",
            "Opsional: tekan Rotate untuk ganti akun/proxy/UA.",
            "Setelah jeda, tekan Retry.",
        ],
        allow_retry=True,
        allow_rotate=True,
        auto_safe=True,
    ),
    "quota_exceeded": Playbook(
        title="📉 Quota habis",
        steps=[
            "Cek kuota API di console platform.",
            "Naikkan kuota atau tunggu reset harian.",
            "Tekan Retry setelah kuota tersedia.",
        ],
        allow_retry=True,
        allow_skip=True,
    ),
    # Network / proxy
    "network_error": Playbook(
        title="🌐 Jaringan/proxy bermasalah",
        steps=[
            "Pastikan koneksi internet/VPN normal.",
            "Set proxy baru via `/secret PROXY_URL=socks5://user:pass@host:port` (atau kosongkan untuk non-proxy).",
            "Tekan Rotate (proxy/UA) lalu Retry.",
        ],
        allow_retry=True,
        allow_rotate=True,
        auto_safe=True,
    ),
    "timeout": Playbook(
        title="⏱ Timeout",
        steps=[
            "Pastikan koneksi stabil dan target URL responsif.",
            "Jika sering timeout, naikkan batas via `/config retry.max_delay=30` atau `/config retry.base_delay=5`.",
            "Tekan Retry.",
        ],
        allow_retry=True,
        auto_safe=True,
    ),
    # Anti-bot / captcha
    "captcha_required": Playbook(
        title="🤖 CAPTCHA/anti-bot",
        steps=[
            "Buka sesi browser (remote) yang diminta bot dan selesaikan CAPTCHA.",
            "Tekan Rotate (UA/proxy) jika masih terdeteksi bot.",
            "Tekan Retry.",
        ],
        allow_retry=True,
        allow_rotate=True,
    ),
    "ban_suspected": Playbook(
        title="🚫 Ban/shadowban dicurigai",
        steps=[
            "Tekan Rotate untuk pakai akun/proxy/UA lain.",
            "Istirahatkan akun lama (jeda >24h).",
            "Tekan Retry dengan akun baru.",
        ],
        allow_retry=True,
        allow_rotate=True,
        allow_skip=True,
        auto_safe=True,
    ),
    # Content/moderation
    "content_rejected": Playbook(
        title="📝 Konten ditolak/moderasi",
        steps=[
            "Kurangi link/CTA berlebihan; hapus kata spam.",
            "Kirim revisi konten sebagai reply atau gunakan /edit jika tersedia.",
            "Tekan Retry.",
        ],
        allow_retry=True,
        auto_safe=True,
    ),
    "visibility_uncertain": Playbook(
        title="👀 Visibilitas tidak pasti",
        steps=[
            "Buka link posting dan cek apakah tampil.",
            "Jika tidak terlihat, tekan Rotate (akun/proxy/UA) lalu Retry.",
        ],
        allow_retry=True,
        allow_rotate=True,
        auto_safe=True,
    ),
    # DB/IO/config
    "db_unreachable": Playbook(
        title="🗄️ DB tidak dapat diakses",
        steps=[
            "Pastikan service DB hidup (cek koneksi/kredensial).",
            "Set ulang URL jika perlu: `/secret DATABASE_URL=postgresql+psycopg2://user:pass@host/db`.",
            "Tekan Retry setelah DB up.",
        ],
        allow_retry=True,
    ),
    "config_invalid": Playbook(
        title="⚙️ Config tidak valid",
        steps=[
            "Perbaiki nilai via `/config key=value` atau `/secret NAME=VALUE` sesuai pesan error.",
            "Pastikan format benar (angka/bool/string).",
            "Tekan Retry.",
        ],
        allow_retry=True,
    ),
    # Default fallback
    "unknown": Playbook(
        title="ℹ️ Langkah pemulihan umum",
        steps=[
            "Tekan Retry.",
            "Jika berulang: tekan Rotate untuk ganti akun/proxy/UA.",
            "Cek log detail via /logs LEVEL (contoh: `/logs ERROR 200`).",
        ],
        allow_retry=True,
        allow_rotate=True,
        allow_skip=True,
        auto_safe=False,
    ),
}


def build_playbook(error_code: Optional[str]) -> Playbook:
    """Return playbook for given error code or generic fallback."""
    if not error_code:
        return _PLAYBOOKS["unknown"]
    normalized = error_code.strip().lower()
    return _PLAYBOOKS.get(normalized, _PLAYBOOKS["unknown"])
