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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from google.oauth2.credentials import Credentials
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


class YouTubeAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, credentials: list[Dict[str, Any]], logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        self.credentials = credentials
        # YouTube Data API v3 has a quota cost of 1 unit per read, 50 units per write
        self.rate_limiter = FixedWindowRateLimiter(max_calls=9000, window_seconds=86400)  # Daily quota
        self._client = None
        self._logged_in_as = None
        self._service = None

    def _create_client(self, account: Dict[str, Any]) -> Any:
        """Create and return an authenticated YouTube client."""
        try:
            creds = Credentials(
                token=account.get('access_token'),
                refresh_token=account.get('refresh_token'),
                token_uri='https://oauth2.googleapis.com/token',
                client_id=account.get('client_id'),
                client_secret=account.get('client_secret')
            )
            
            # Build the YouTube API client
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
            self._service = self._create_client(account)
            # Test the connection by getting channel info
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
                
            # First, get the uploads playlist ID for the channel
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
                    'count': len(videos)
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

    def post_comment(self, video_id: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        """Post a comment to a YouTube video."""
        try:
            if not self._service:
                raise AuthenticationError("Not authenticated with YouTube")
                
            # First, check if we need to log in with a different account
            if account.get('username') != self._logged_in_as:
                login_result = self.login(account)
                if not login_result.success:
                    return login_result
            
            # Post the comment
            comment_response = self._call_with_limits(
                self._service.commentThreads().insert,
                part='snippet',
                body={
                    'snippet': {
                        'videoId': video_id,
                        'topLevelComment': {
                            'snippet': {
                                'textOriginal': content
                            }
                        }
                    }
                }
            )
            
            return AdapterResult(
                success=True,
                data={
                    'comment_id': comment_response['id'],
                    'video_id': video_id,
                    'url': f"https://www.youtube.com/watch?v={video_id}&lc={comment_response['id']}"
                }
            )
            
        except Exception as exc:
            self.logger.error(
                "Failed to post comment", 
                extra={"component": "youtube_adapter", "error": str(exc), "video_id": video_id}
            )
            return AdapterResult(
                success=False, 
                data={"video_id": video_id}, 
                error=str(exc), 
                retry_recommended=not isinstance(exc, AuthenticationError)
            )

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
            return AdapterResult(success=True, data={"health_score": 0.5, "issues": ["not_implemented"]})
        except Exception as exc:
            self.logger.error(
                "YouTube adapter error", 
                extra={"component": "youtube_adapter", "error": str(exc)}
            )
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def close(self) -> None:
        return
