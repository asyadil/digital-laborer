"""Quora automation adapter using Selenium.

This adapter handles interactions with Quora including:
- Authentication via email/password
- Finding questions in specified topics
- Posting answers to questions
- Retrieving answer metrics
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementClickInterceptedException,
    WebDriverException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.platforms.base_adapter import (
    AdapterResult,
    AntiBotChallengeError,
    AuthenticationError,
    BasePlatformAdapter,
    PlatformAdapterError,
    RateLimitError,
)
from src.platforms.selenium_session import SeleniumSession
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.retry import retry_with_exponential_backoff


class QuoraAdapter(BasePlatformAdapter):
    BASE_URL = 'https://www.quora.com'
    LOGIN_URL = f'{BASE_URL}/login'
    
    def __init__(self, config: Any, credentials: list[Dict[str, Any]], logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        self.credentials = credentials
        self.rate_limiter = FixedWindowRateLimiter(max_calls=50, window_seconds=3600)  # 50 requests/hour
        self._session: Optional[SeleniumSession] = None
        self._logged_in_as: Optional[str] = None
        self._browser_type = config.get('browser', 'chrome')
        self._headless = config.get('headless', True)
        self._timeout = config.get('timeout_seconds', 30)

    def _init_session(self) -> None:
        """Initialize Selenium session if not already done."""
        if self._session is None:
            self._session = SeleniumSession(
                browser_type=self._browser_type,
                headless=self._headless,
                logger=self.logger
            )
            self._session.set_page_load_timeout(self._timeout)
    
    def _wait_for_element(self, by: str, selector: str, timeout: int = None) -> WebElement:
        """Wait for an element to be present on the page."""
        timeout = timeout or self._timeout
        return WebDriverWait(self._session.driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )
    
    def _wait_for_element_clickable(self, by: str, selector: str, timeout: int = None) -> WebElement:
        """Wait for an element to be clickable."""
        timeout = timeout or self._timeout
        return WebDriverWait(self._session.driver, timeout).until(
            EC.element_to_be_clickable((by, selector))
        )
    
    def _is_logged_in(self) -> bool:
        """Check if we're logged in by looking for the user avatar."""
        try:
            self._wait_for_element(
                By.CSS_SELECTOR, 
                'div[aria-label*="Profile"][role="button"]', 
                timeout=5
            )
            return True
        except (NoSuchElementException, TimeoutException):
            return False
    
    def _handle_captcha(self) -> None:
        """Handle CAPTCHA challenges if they appear."""
        try:
            # Check for CAPTCHA iframe
            captcha_frame = self._wait_for_element(
                By.CSS_SELECTOR, 
                'iframe[src*="recaptcha"]',
                timeout=5
            )
            if captcha_frame:
                raise AntiBotChallengeError(
                    "CAPTCHA detected. Please solve it manually and retry."
                )
        except (NoSuchElementException, TimeoutException):
            pass
    
    def _login_with_credentials(self, email: str, password: str) -> bool:
        """Perform the login process with email and password."""
        try:
            self._session.driver.get(self.LOGIN_URL)
            
            # Wait for login form
            email_field = self._wait_for_element(
                By.CSS_SELECTOR, 
                'input[name="email"]',
                timeout=10
            )
            
            # Enter credentials
            email_field.clear()
            email_field.send_keys(email)
            
            password_field = self._wait_for_element(
                By.CSS_SELECTOR, 
                'input[name="password"]',
                timeout=5
            )
            password_field.clear()
            password_field.send_keys(password)
            
            # Click login button
            login_button = self._wait_for_element_clickable(
                By.CSS_SELECTOR, 
                'button[type="submit"]',
                timeout=5
            )
            login_button.click()
            
            # Wait for login to complete
            time.sleep(5)  # Wait for potential redirects
            
            # Check for login success
            if not self._is_logged_in():
                self._handle_captcha()
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(
                "Error during login", 
                extra={"error": str(e), "email": email}
            )
            return False
    
    @retry_with_exponential_backoff(max_attempts=3, base_delay=5)
    def login(self, account: Dict[str, Any]) -> AdapterResult:
        """Log in to Quora with the provided account credentials."""
        self._init_session()
        
        try:
            # Check if already logged in
            if self._is_logged_in():
                self._logged_in_as = account.get('email')
                return AdapterResult(
                    success=True, 
                    data={"username": self._logged_in_as}
                )
            
            # Attempt login
            email = account.get('email')
            password = account.get('password')
            
            if not email or not password:
                raise AuthenticationError("Missing email or password in account credentials")
            
            login_success = self._login_with_credentials(email, password)
            
            if not login_success:
                raise AuthenticationError("Login failed. Check your credentials or solve CAPTCHA.")
            
            self._logged_in_as = email
            return AdapterResult(
                success=True, 
                data={"username": self._logged_in_as}
            )
            
        except PlatformAdapterError:
            raise  # Re-raise known errors
        except Exception as exc:
            self.logger.error(
                "Quora login error", 
                extra={"component": "quora_adapter", "error": str(exc)}
            )
            raise AuthenticationError(f"Login failed: {str(exc)}")

    def _scroll_to_bottom(self):
        """Scroll to the bottom of the page to load more content."""
        self._session.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight);"
        )
        time.sleep(2)  # Wait for content to load

    def _find_questions_by_topic(self, topic: str, max_questions: int = 10) -> List[Dict[str, Any]]:
        """Find questions related to a specific topic.
        
        Args:
            topic: The topic to search for
            max_questions: Maximum number of questions to return
            
        Returns:
            List of question dictionaries with 'url' and 'text' keys
        """
        if not self._is_logged_in():
            raise AuthenticationError("Not logged in to Quora")
            
        try:
            # Navigate to topic page
            search_url = f"{self.BASE_URL}/search?q={topic.replace(' ', '+')}&type=question"
            self._session.driver.get(search_url)
            time.sleep(3)  # Wait for results to load
            
            questions = []
            seen_questions = set()
            
            while len(questions) < max_questions:
                # Scroll to load more questions
                self._scroll_to_bottom()
                
                # Find question elements
                question_elements = self._session.driver.find_elements(
                    By.CSS_SELECTOR, 
                    'a[class*="q-box"][href*="/question/"]'
                )
                
                for elem in question_elements:
                    try:
                        url = elem.get_attribute('href')
                        text = elem.text.strip()
                        
                        if url and text and url not in seen_questions:
                            seen_questions.add(url)
                            questions.append({
                                'url': url,
                                'text': text,
                                'topic': topic
                            })
                            
                            if len(questions) >= max_questions:
                                return questions
                                
                    except Exception as e:
                        self.logger.warning(
                            "Error processing question element",
                            extra={"error": str(e)}
                        )
                        continue
                
                # Check if we can load more
                try:
                    load_more = self._session.driver.find_element(
                        By.CSS_SELECTOR, 
                        'button[class*="more_questions"]'
                    )
                    load_more.click()
                    time.sleep(2)
                except NoSuchElementException:
                    break  # No more questions to load
                    
            return questions[:max_questions]
            
        except Exception as e:
            self.logger.error(
                "Error finding questions",
                extra={"topic": topic, "error": str(e)}
            )
            raise PlatformAdapterError(f"Failed to find questions: {str(e)}")

    @retry_with_exponential_backoff(max_attempts=3, base_delay=10)
    def post_answer(self, question_url: str, content: str, **kwargs) -> AdapterResult:
        """Post an answer to a Quora question.
        
        Args:
            question_url: URL of the question to answer
            content: The answer content (can include HTML formatting)
            **kwargs: Additional options
                - include_referral: Whether to include a referral link
                - referral_link: Custom referral link to include
                
        Returns:
            AdapterResult with success status and response data
        """
        if not self._is_logged_in():
            raise AuthenticationError("Not logged in to Quora")
            
        try:
            self.logger.info(f"Posting answer to: {question_url}")
            
            # Navigate to question
            self._session.driver.get(question_url)
            time.sleep(3)  # Wait for page to load
            
            # Click "Answer" button
            try:
                answer_button = self._wait_for_element_clickable(
                    By.CSS_SELECTOR,
                    'div[class*="q-click-wrapper"][role="button"]',
                    timeout=10
                )
                answer_button.click()
            except TimeoutException:
                # If answer button not found, try direct URL
                answer_url = f"{question_url}?answer=1"
                self._session.driver.get(answer_url)
                time.sleep(3)
            
            # Switch to answer iframe if present
            try:
                iframe = self._wait_for_element(
                    By.TAG_NAME, 'iframe', timeout=5
                )
                self._session.driver.switch_to.frame(iframe)
            except TimeoutException:
                pass  # No iframe, continue with normal flow
            
            # Find and fill the answer box
            answer_box = self._wait_for_element(
                By.CSS_SELECTOR,
                'div[role="textbox"][contenteditable="true"]',
                timeout=10
            )
            
            # Clear any existing text
            answer_box.clear()
            
            # Type the content
            answer_box.send_keys(content)
            
            # Handle referral link if needed
            if kwargs.get('include_referral', False):
                self._add_referral_link(
                    kwargs.get('referral_link', self.config.get('default_referral_link'))
                )
            
            # Click "Post" button
            post_button = self._wait_for_element_clickable(
                By.CSS_SELECTOR,
                'div[class*="q-click-wrapper"][role="button"]:not([disabled])',
                timeout=10
            )
            post_button.click()
            
            # Wait for success message or error
            try:
                success_element = self._wait_for_element(
                    By.XPATH,
                    '//*[contains(text(), "Your answer has been posted")]',
                    timeout=10
                )
                
                # Get the URL of the posted answer
                answer_url = self._session.driver.current_url
                
                self.logger.info(
                    "Successfully posted answer",
                    extra={"question_url": question_url, "answer_url": answer_url}
                )
                
                return AdapterResult(
                    success=True,
                    data={
                        'answer_url': answer_url,
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    }
                )
                
            except TimeoutException:
                # Check for error message
                try:
                    error_element = self._session.driver.find_element(
                        By.CSS_SELECTOR, 
                        'div[class*="error"]'
                    )
                    error_msg = error_element.text.strip()
                    raise PlatformAdapterError(f"Failed to post answer: {error_msg}")
                except NoSuchElementException:
                    pass  # No error element found
                
                # If we got here, we're not sure what happened
                raise PlatformAdapterError("Failed to verify if answer was posted")
                
        except Exception as e:
            self.logger.error(
                "Error posting answer",
                extra={"question_url": question_url, "error": str(e)}
            )
            return AdapterResult(
                success=False,
                error=str(e),
                retry_recommended=not isinstance(e, (AuthenticationError, PlatformAdapterError))
            )
    
    def _add_referral_link(self, referral_link: str) -> None:
        """Add a referral link to the current answer."""
        try:
            # Click the link button
            link_button = self._wait_for_element_clickable(
                By.CSS_SELECTOR,
                'button[aria-label="Add link"]',
                timeout=5
            )
            link_button.click()
            
            # Enter the URL
            url_input = self._wait_for_element(
                By.NAME, 'url', timeout=5
            )
            url_input.clear()
            url_input.send_keys(referral_link)
            
            # Click Add button
            add_button = self._session.driver.find_element(
                By.XPATH,
                '//button[contains(@class, "q-click-wrapper") and .//*[text()="Add"]]'
            )
            add_button.click()
            
        except Exception as e:
            self.logger.warning(
                "Failed to add referral link",
                extra={"error": str(e)}
            )
            # Continue without the referral link rather than failing the whole operation

    def get_question_metrics(self, question_url: str) -> Dict[str, Any]:
        """Get metrics for a specific question.
        
        Args:
            question_url: URL of the question
            
        Returns:
            Dictionary containing question metrics
        """
        if not self._is_logged_in():
            raise AuthenticationError("Not logged in to Quora")
            
        try:
            self._session.driver.get(question_url)
            time.sleep(3)  # Wait for page to load
            
            metrics = {
                'url': question_url,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            
            # Get question text
            try:
                question_text = self._wait_for_element(
                    By.CSS_SELECTOR, 'div[class*="question_text"]',
                    timeout=5
                ).text.strip()
                metrics['question_text'] = question_text
            except (NoSuchElementException, TimeoutException):
                pass
                
            # Get number of answers
            try:
                answers_text = self._session.driver.find_element(
                    By.CSS_SELECTOR, 'div[class*="answer_count"]'
                ).text
                metrics['answer_count'] = int(''.join(filter(str.isdigit, answers_text)))
            except (NoSuchElementException, ValueError):
                metrics['answer_count'] = 0
                
            # Get number of followers
            try:
                followers_text = self._session.driver.find_element(
                    By.CSS_SELECTOR, 'div[class*="follower_count"]'
                ).text
                metrics['follower_count'] = int(''.join(filter(str.isdigit, followers_text)))
            except (NoSuchElementException, ValueError):
                metrics['follower_count'] = 0
                
            # Get related topics
            try:
                topic_elements = self._session.driver.find_elements(
                    By.CSS_SELECTOR, 'a[class*="TopicNameSpan"]'
                )
                metrics['topics'] = [t.text.strip() for t in topic_elements if t.text.strip()]
            except NoSuchElementException:
                metrics['topics'] = []
                
            return metrics
            
        except Exception as e:
            self.logger.error(
                "Error getting question metrics",
                extra={"question_url": question_url, "error": str(e)}
            )
            raise PlatformAdapterError(f"Failed to get question metrics: {str(e)}")

    def close(self) -> None:
        """Clean up resources."""
        try:
            if self._session:
                self._session.quit()
                self._session = None
            self._logged_in_as = None
        except Exception as e:
            self.logger.error("Error during cleanup", extra={"error": str(e)})

    def _scroll_to_bottom(self):
        """Scroll to the bottom of the page to load more content."""
        self._session.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight);"
        )
        time.sleep(2)  # Wait for content to load

    def _get_question_links(self, topic_url: str, limit: int) -> List[Dict[str, str]]:
        """Extract question links from a topic page."""
        self._session.driver.get(topic_url)
        
        # Wait for questions to load
        self._wait_for_element(
            By.CSS_SELECTOR,
            'div.puppeteer_test_question_title',
            timeout=10
        )
        
        questions = []
        seen_questions = set()
        
        while len(questions) < limit:
            # Find all question elements
            question_elements = self._session.driver.find_elements(
                By.CSS_SELECTOR,
                'div.puppeteer_test_question_title a[href*="/question/"]'
            )
            
            # Extract question data
            for elem in question_elements:
                if len(questions) >= limit:
                    break
                    
                try:
                    url = elem.get_attribute('href')
                    text = elem.text.strip()
                    
                    if url and url not in seen_questions:
                        question_id = url.split('/')[-1]
                        questions.append({
                            'id': question_id,
                            'title': text,
                            'url': url,
                            'topic': topic_url.split('/')[-1]
                        })
                        seen_questions.add(url)
                except Exception as e:
                    self.logger.warning(
                        "Error extracting question",
                        extra={"error": str(e)}
                    )
            
            # Break if we've found enough questions
            if len(questions) >= limit:
                break
                
            # Scroll to load more questions
            try:
                self._scroll_to_bottom()
            except Exception as e:
                self.logger.warning(
                    "Error scrolling page",
                    extra={"error": str(e)}
                )
                break
        
        return questions[:limit]
    
    @retry_with_exponential_backoff(max_attempts=2, base_delay=5)
    def find_target_posts(self, topic: str, limit: int = 10) -> AdapterResult:
        """Find recent questions in the specified topic."""
        try:
            if not self._session or not self._is_logged_in():
                return AdapterResult(
                    success=False,
                    data={"items": [], "topic": topic, "limit": limit},
                    error="Not authenticated with Quora",
                    retry_recommended=True
                )
            
            # Format topic URL
            topic_url = f"{self.BASE_URL}/topic/{topic}/all_questions"
            
            with self.rate_limiter:
                questions = self._get_question_links(topic_url, limit)
            
            return AdapterResult(
                success=True,
                data={
                    'items': questions,
                    'topic': topic,
                    'limit': len(questions)
                }
            )
            
        except Exception as exc:
            self.logger.error(
                "Failed to find target questions", 
                extra={"component": "quora_adapter", "error": str(exc), "topic": topic}
            )
            return AdapterResult(
                success=False, 
                data={"topic": topic, "limit": limit}, 
                error=str(exc), 
                retry_recommended=not isinstance(exc, AuthenticationError)
            )

    @retry_with_exponential_backoff(max_attempts=2, base_delay=10)
    def post_comment(self, question_url: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        """Post an answer to a Quora question."""
        try:
            # Ensure we're logged in with the correct account
            if account.get('email') != self._logged_in_as:
                login_result = self.login(account)
                if not login_result.success:
                    return login_result
            
            with self.rate_limiter:
                # Navigate to the question page
                self._session.driver.get(question_url)
                
                # Wait for answer box
                answer_button = self._wait_for_element_clickable(
                    By.CSS_SELECTOR,
                    'div[data-testid="answer_button"]',
                    timeout=10
                )
                answer_button.click()
                
                # Switch to answer iframe
                time.sleep(2)  # Wait for iframe to load
                iframe = self._wait_for_element(
                    By.CSS_SELECTOR,
                    'iframe[title*="Rich Text Editor"]',
                    timeout=10
                )
                self._session.driver.switch_to.frame(iframe)
                
                # Enter answer content
                editor = self._wait_for_element(
                    By.CSS_SELECTOR,
                    'body',
                    timeout=10
                )
                editor.clear()
                editor.send_keys(content)
                
                # Switch back to main content
                self._session.driver.switch_to.default_content()
                
                # Click submit button
                submit_button = self._wait_for_element_clickable(
                    By.CSS_SELECTOR,
                    'button[type="submit"]',
                    timeout=5
                )
                submit_button.click()
                
                # Wait for success or error
                time.sleep(5)  # Wait for submission to complete
                
                # Extract answer URL if successful
                answer_url = self._session.driver.current_url
                answer_id = answer_url.split('/')[-1] if 'answer/' in answer_url else None
                
                if not answer_id:
                    raise PlatformAdapterError("Failed to post answer: Could not determine answer ID")
                
                return AdapterResult(
                    success=True,
                    data={
                        'answer_id': answer_id,
                        'url': answer_url,
                        'question_url': question_url
                    }
                )
                
        except Exception as exc:
            self.logger.error(
                "Failed to post answer", 
                extra={"component": "quora_adapter", "error": str(exc), "question_url": question_url}
            )
            return AdapterResult(
                success=False, 
                data={"question_url": question_url}, 
                error=str(exc), 
                retry_recommended=not isinstance(exc, (AuthenticationError, AntiBotChallengeError))
            )

    def get_comment_metrics(self, answer_url: str) -> AdapterResult:
        """Get metrics for a specific answer."""
        try:
            if not self._session or not self._is_logged_in():
                return AdapterResult(
                    success=False,
                    data={"answer_url": answer_url},
                    error="Not authenticated with Quora",
                    retry_recommended=True
                )
            
            with self.rate_limiter:
                self._session.driver.get(answer_url)
                
                # Wait for answer to load
                self._wait_for_element(
                    By.CSS_SELECTOR,
                    'div.answer_content',
                    timeout=10
                )
                
                # Extract metrics
                try:
                    # Upvotes
                    upvote_element = self._wait_for_element(
                        By.CSS_SELECTOR,
                        'span[data-testid="social_count"]',
                        timeout=5
                    )
                    upvotes_text = upvote_element.text.strip()
                    upvotes = int(''.join(filter(str.isdigit, upvotes_text)) or 0)
                except (NoSuchElementException, TimeoutException):
                    upvotes = 0
                
                # View count (approximate)
                try:
                    view_element = self._session.driver.find_element(
                        By.CSS_SELECTOR,
                        'div.AnswerFooter span.answer_views'
                    )
                    views_text = view_element.text.strip()
                    views = int(''.join(filter(str.isdigit, views_text)) or 0)
                except (NoSuchElementException, ValueError):
                    views = 0
                
                # Answer content
                try:
                    content_element = self._session.driver.find_element(
                        By.CSS_SELECTOR,
                        'div.answer_content div[data-testid="answer_content"]'
                    )
                    content = content_element.text.strip()
                except NoSuchElementException:
                    content = ""
                
                # Check if answer is deleted or removed
                is_visible = not bool(
                    self._session.driver.find_elements(
                        By.CSS_SELECTOR,
                        'div.answer_wrapper.deleted, div.answer_wrapper.collapsed'
                    )
                )
                
                return AdapterResult(
                    success=True,
                    data={
                        'answer_url': answer_url,
                        'upvotes': upvotes,
                        'views': views,
                        'is_visible': is_visible,
                        'content_preview': content[:200] + '...' if content else '',
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    }
                )
                
        except Exception as exc:
            self.logger.error(
                "Failed to get answer metrics", 
                extra={"component": "quora_adapter", "error": str(exc), "answer_url": answer_url}
            )
            return AdapterResult(
                success=False, 
                data={"answer_url": answer_url}, 
                error=str(exc), 
                retry_recommended=True
            )
    
    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        """Check if the account is in good standing."""
        try:
            if not self._session:
                self._init_session()
                
            # Try to access the account profile
            self._session.driver.get(f"{self.BASE_URL}/profile/{account.get('username')}")
            
            # Check for account warnings or restrictions
            warning_elements = self._session.driver.find_elements(
                By.CSS_SELECTOR,
                'div.account_warning, div.account_restricted'
            )
            
            issues = []
            if warning_elements:
                issues.append('account_warning')
            
            # Check if account can post
            can_post = True
            try:
                test_post_url = f"{self.BASE_URL}/question/Test-Post-{int(time.time())}"
                self._session.driver.get(test_post_url)
                answer_button = self._wait_for_element_clickable(
                    By.CSS_SELECTOR,
                    'div[data-testid="answer_button"]',
                    timeout=5
                )
            except (NoSuchElementException, TimeoutException):
                can_post = False
                issues.append('posting_restricted')
            
            return AdapterResult(
                success=True,
                data={
                    'health_score': 0.8 if can_post and not issues else 0.3,
                    'issues': issues,
                    'can_post': can_post,
                    'last_checked': datetime.utcnow().isoformat()
                }
            )
            
        except Exception as exc:
            self.logger.error(
                "Account health check failed",
                extra={"component": "quora_adapter", "error": str(exc), "account": account.get('email')}
            )
            return AdapterResult(
                success=False,
                data={"account": account.get('email', 'unknown')},
                error=str(exc),
                retry_recommended=True
            )
    
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def close(self) -> None:
        return
