"""YouTube automation adapter using YouTube Data API v3.

This adapter handles interactions with YouTube including:
- Authentication via OAuth 2.0
- Finding target videos in specified channels
- Posting comments on videos
- Retrieving comment metrics
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.platforms.base_adapter import (
    AdapterResult,
    AntiBotChallengeError,
    AuthenticationError,
    BasePlatformAdapter,
    PlatformAdapterError,
    RateLimitError,
)
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.retry import retry_with_exponential_backoff
from src.platforms.captcha_handler import CaptchaHandler
from src.utils.user_agents import pick_random_user_agent


class YouTubeAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, credentials: list[Dict[str, Any]], logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        self.credentials = credentials
        # YouTube Data API v3 has a quota cost of 1 unit per read, 50 units per write
        self.rate_limiter = FixedWindowRateLimiter(max_calls=9000, window_seconds=86400)  # Daily quota
        self._client = None
        self._logged_in_as = None
        self._service = None
        self.captcha_handler = CaptchaHandler(telegram) if telegram else None
        self._jitter_range_ms = (400, 1200)
        self._proxy_pool = getattr(getattr(config, "platforms", None), "youtube", {}).get("proxies", []) if hasattr(config, "platforms") else []
        self._ua_pool = getattr(getattr(config, "platforms", None), "youtube", {}).get("user_agents", []) if hasattr(config, "platforms") else []
        self._current_proxy: Optional[str] = None
        self._current_ua: Optional[str] = None

    def _create_client(self, account: Dict[str, Any]) -> Any:
        """Create and return an authenticated YouTube client."""
        try:
            self._rotate_identity()
            creds = Credentials(
                token=account.get('access_token'),
                refresh_token=account.get('refresh_token'),
                token_uri='https://oauth2.googleapis.com/token',
                client_id=account.get('client_id'),
                client_secret=account.get('client_secret')
            )
            # Force refresh if token expired
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            youtube = build('youtube', 'v3', credentials=creds)
            return youtube
        except Exception as exc:
            self.logger.error("Failed to create YouTube client", extra={"error": str(exc)})
            raise AuthenticationError(f"YouTube authentication failed: {str(exc)}")

    def _call_with_limits(self, func, *args, **kwargs):
        """Wrapper to handle rate limiting and retries for API calls."""
        @retry_with_exponential_backoff(
            max_attempts=3,
            base_delay=2,
            exceptions=(RateLimitError,)
        )
        def _wrapped():
            with self.rate_limiter:
                try:
                    return func(*args, **kwargs)
                except HttpError as e:
                    if e.resp.status == 403 and 'quotaExceeded' in str(e):
                        raise RateLimitError("YouTube API quota exceeded")
                    elif e.resp.status == 403 and 'quota' in str(e).lower():
                        raise RateLimitError("YouTube API quota limit reached")
                    elif e.resp.status == 403:
                        raise AuthenticationError(f"YouTube API access denied: {str(e)}")
                    elif e.resp.status == 429:
                        retry_after = int(e.resp.get('retry-after', 60))
                        raise RateLimitError(f"Rate limited by YouTube. Retry after {retry_after} seconds")
                    else:
                        raise PlatformAdapterError(f"YouTube API error: {str(e)}")
        
        return _wrapped()

    def login(self, account: Dict[str, Any]) -> AdapterResult:
        """Authenticate with YouTube using OAuth 2.0 credentials."""
        try:
            self._human_pause()
            self._service = self._create_client(account)
            channels = self._call_with_limits(
                self._service.channels().list,
                part='snippet',
                mine=True,
                maxResults=1
            )
            
            if not channels.get('items'):
                raise AuthenticationError("No YouTube channel found for this account")
                
            self._logged_in_as = channels['items'][0]['snippet']['title']
            return AdapterResult(
                success=True, 
                data={"username": self._logged_in_as, "channel_id": channels['items'][0]['id']}
            )
            
        except PlatformAdapterError as exc:
            return AdapterResult(
                success=False, 
                data={"account": account.get('username', 'unknown')}, 
                error=str(exc), 
                retry_recommended=isinstance(exc, RateLimitError)
            )
        except Exception as exc:
            # Try 2FA rescue if available
            if self.captcha_handler:
                try:
                    code = self.captcha_handler.handle_2fa_sync(platform="youtube", method="app", timeout=300)
                    if code.solved and code.response:
                        account = {**account, "otp": code.response}
                        self._service = self._create_client(account)
                        channels = self._call_with_limits(
                            self._service.channels().list,
                            part='snippet',
                            mine=True,
                            maxResults=1
                        )
                        if channels.get('items'):
                            self._logged_in_as = channels['items'][0]['snippet']['title']
                            return AdapterResult(
                                success=True,
                                data={"username": self._logged_in_as, "channel_id": channels['items'][0]['id']}
                            )
                except Exception:
                    pass
            self.logger.error(
                "YouTube login failed", 
                extra={"component": "youtube_adapter", "error": str(exc)}
            )
            return AdapterResult(
                success=False, 
                data={"account": account.get('username', 'unknown')}, 
                error=str(exc), 
                retry_recommended=True
            )

    def find_target_posts(self, channel_id: str, limit: int = 10) -> AdapterResult:
        """Find recent videos in the specified channel."""
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")
            self._human_pause()
                
            # First, get the uploads playlist ID for the channel
            start = time.monotonic()
            channels_response = self._call_with_limits(
                self._service.channels().list,
                part='contentDetails',
                id=channel_id,
                maxResults=1
            )
            
            if not channels_response.get('items'):
                return AdapterResult(
                    success=False,
                    data={"items": [], "channel_id": channel_id, "limit": limit},
                    error=f"Channel {channel_id} not found",
                    retry_recommended=False
                )
            
            uploads_playlist_id = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            
            # Get videos from the uploads playlist
            playlist_items = self._call_with_limits(
                self._service.playlistItems().list,
                part='snippet',
                playlistId=uploads_playlist_id,
                maxResults=limit
            )
            
            videos = []
            for item in playlist_items.get('items', []):
                video = {
                    'id': item['snippet']['resourceId']['videoId'],
                    'title': item['snippet']['title'],
                    'url': f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}",
                    'published_at': item['snippet']['publishedAt'],
                    'channel_id': channel_id,
                    'channel_title': item['snippet'].get('channelTitle', '')
                }
                videos.append(video)
            
            return AdapterResult(
                success=True,
                data={
                    'items': videos,
                    'channel_id': channel_id,
                    'limit': limit,
                    'duration_ms': round((time.monotonic() - start) * 1000, 2),
                }
            )
            
        except Exception as exc:
            self.logger.error(
                "Failed to find target videos", 
                extra={"component": "youtube_adapter", "error": str(exc)}
            )
            return AdapterResult(
                success=False, 
                data={"channel_id": channel_id, "limit": limit}, 
                error=str(exc), 
                retry_recommended=True
            )

    def post_comment(self, video_id: str, content: str) -> AdapterResult:
        """Post a comment to a YouTube video."""
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")
            self._human_pause()
            
            if not video_id:
                raise ValueError("video_id is required")
            
            if not content or not content.strip():
                raise ValueError("content is empty")
            
            comment_body = {
                'snippet': {
                    'videoId': video_id,
                    'topLevelComment': {
                        'snippet': {
                            'textOriginal': content
                        }
                    }
                }
            }
            
            start = time.monotonic()
            response = self._call_with_limits(
                self._service.commentThreads().insert,
                part='snippet',
                body=comment_body
            )
            
            comment_id = response['id']
            comment_url = f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
            
            return AdapterResult(
                success=True,
                data={
                    'comment_id': comment_id,
                    'comment_url': comment_url,
                    'duration_ms': round((time.monotonic() - start) * 1000, 2),
                }
            )
            
        except Exception as exc:
            shot = None
            try:
                if not os.getenv("PYTEST_CURRENT_TEST"):
                    if not hasattr(self, "_screenshot_dir"):
                        self._screenshot_dir = "screenshots"
                    os.makedirs(self._screenshot_dir, exist_ok=True)
                    fname = f"youtube_error_{video_id or 'unknown'}_{int(time.time())}.log"
                    fpath = os.path.join(self._screenshot_dir, fname)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(str(exc))
                    shot = fpath
            except Exception:
                shot = None
            self.logger.error(
                "YouTube post_comment error",
                extra={"component": "youtube_adapter", "video_id": video_id, "error": str(exc), "screenshot": shot}
            )
            return AdapterResult(success=False, data={'video_id': video_id, "screenshot": shot} if shot else {'video_id': video_id}, error=str(exc), retry_recommended=True)

    def get_comment_metrics(self, comment_id: str) -> AdapterResult:
        """Get metrics for a specific comment."""
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")
                
            # Extract comment ID from URL if a full URL was provided
            if 'youtube.com' in comment_id:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(comment_id)
                if 'lc=' in parsed.query:
                    comment_id = parse_qs(parsed.query)['lc'][0]
                elif 'comment_id=' in parsed.query:
                    comment_id = parse_qs(parsed.query)['comment_id'][0]
            
            # Get the comment details
            comment = self._call_with_limits(
                self._service.comments().list,
                part='snippet,id',
                id=comment_id,
                maxResults=1
            )
            
            if not comment.get('items'):
                return AdapterResult(
                    success=False,
                    data={"comment_id": comment_id},
                    error="Comment not found",
                    retry_recommended=False
                )
            
            snippet = comment['items'][0]['snippet']
            
            return AdapterResult(
                success=True,
                data={
                    'comment_id': comment_id,
                    'like_count': int(snippet.get('likeCount', 0)),
                    'reply_count': 0,  # Would require additional API call to threads
                    'is_public': snippet.get('moderationStatus', 'published') == 'published',
                    'author': snippet.get('authorDisplayName', ''),
                    'text': snippet.get('textOriginal', ''),
                    'published_at': snippet.get('publishedAt', ''),
                    'updated_at': snippet.get('updatedAt', '')
                }
            )
            
        except Exception as exc:
            self.logger.error(
                "Failed to get comment metrics", 
                extra={"component": "youtube_adapter", "error": str(exc), "comment_id": comment_id}
            )
            return AdapterResult(
                success=False, 
                data={"comment_id": comment_id}, 
                error=str(exc), 
                retry_recommended=True
            )

    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")

            # Fetch channel stats
            start = time.monotonic()
            stats = self._call_with_limits(
                self._service.channels().list,
                part="statistics,snippet",
                mine=True,
                maxResults=1,
            )
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            if not stats.get("items"):
                return AdapterResult(
                    success=False,
                    data={"issues": ["channel_not_found"], "duration_ms": duration_ms},
                    error="No channel stats available",
                    retry_recommended=False,
                )

            item = stats["items"][0]
            statistics = item.get("statistics", {})
            subs = int(statistics.get("subscriberCount", 0))
            views = int(statistics.get("viewCount", 0))
            videos = int(statistics.get("videoCount", 0))
            issues: List[str] = []
            health_score = 0.4
            if subs >= 100:
                health_score += 0.2
            if views >= 10000:
                health_score += 0.2
            if videos >= 10:
                health_score += 0.1
            if subs < 10:
                issues.append("low_subscribers")
            if videos < 3:
                issues.append("low_video_count")

            return AdapterResult(
                success=True,
                data={
                    "health_score": max(0.0, min(1.0, health_score)),
                    "issues": issues,
                    "subscribers": subs,
                    "views": views,
                    "videos": videos,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as exc:
            self.logger.error(
                "YouTube adapter error", 
                extra={"component": "youtube_adapter", "error": str(exc)}
            )
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def _calculate_relevance(self, item: Dict[str, Any], keyword: str) -> float:
        """Calculate a relevance score based on title/description match."""
        title = item.get("title", "").lower()
        desc = item.get("description", "").lower()
        kw = keyword.lower()
        score = 0.0
        if kw in title:
            score += 0.5
        if kw in desc:
            score += 0.3
        if title.startswith(kw):
            score += 0.2
        return min(1.0, score)

    def search_videos_by_keywords(self, keywords: List[str], limit: int = 20, published_after: Optional[str] = None) -> AdapterResult:
        """Search YouTube videos by keywords using the search endpoint."""
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")
            items: List[Dict[str, Any]] = []
            seen_ids: set[str] = set()
            for keyword in keywords:
                search_response = self._call_with_limits(
                    self._service.search().list,
                    part="snippet",
                    q=keyword,
                    type="video",
                    maxResults=min(limit, 50),
                    order="relevance",
                    publishedAfter=published_after,
                    relevanceLanguage="en",
                    videoCaption="any",
                )
                for item in search_response.get("items", []):
                    vid = item["id"]["videoId"]
                    if vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    snippet = item.get("snippet", {})
                    video = {
                        "id": vid,
                        "title": snippet.get("title", ""),
                        "description": snippet.get("description", ""),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "published_at": snippet.get("publishedAt"),
                        "channel_id": snippet.get("channelId"),
                        "channel_title": snippet.get("channelTitle"),
                        "relevance": self._calculate_relevance({"title": snippet.get("title",""), "description": snippet.get("description","")}, keyword),
                    }
                    items.append(video)
            # sort by relevance desc
            items.sort(key=lambda x: x.get("relevance", 0), reverse=True)
            return AdapterResult(
                success=True,
                data={"items": items[:limit], "count": len(items[:limit])},
            )
        except Exception as exc:
            self.logger.error(
                "YouTube search failed",
                extra={"component": "youtube_adapter", "error": str(exc)},
            )
            return AdapterResult(
                success=False,
                data={"items": []},
                error=str(exc),
                retry_recommended=True,
            )

    def close(self) -> None:
        return
