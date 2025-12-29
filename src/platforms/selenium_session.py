"""Selenium session manager with undetected-chromedriver.

Provides:
- Consistent browser automation with Chrome
- User-agent and proxy rotation
- Screenshot capabilities
- Advanced element interaction
- Session management
"""
from __future__ import annotations

import logging
import os
import time
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Configure logger
logger = logging.getLogger(__name__)


@dataclass
class SeleniumSessionConfig:
    # Browser configuration
    headless: bool = False
    disable_images: bool = True
    disable_javascript: bool = False
    disable_webgl: bool = True
    disable_extensions: bool = True
    disable_notifications: bool = True
    disable_popup_blocking: bool = True
    disable_infobars: bool = True
    disable_gpu: bool = True
    no_sandbox: bool = True
    disable_dev_shm_usage: bool = True
    
    # Window settings
    window_width: int = 1280
    window_height: int = 900
    
    # Timeouts
    page_load_timeout: int = 30
    script_timeout: int = 20
    implicit_wait: int = 5
    
    # User data and profiles
    user_data_dir: Optional[str] = None
    profile_dir: Optional[str] = None
    
    # Proxy settings
    proxy: Optional[str] = None
    proxy_auth: Optional[Dict[str, str]] = None
    
    # User agent
    user_agent: Optional[str] = None
    
    # Additional arguments
    experimental_options: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        # Set default user data directory if not specified
        if self.user_data_dir is None:
            self.user_data_dir = str(Path.home() / '.browser_profiles' / 'default')
            os.makedirs(self.user_data_dir, exist_ok=True)


class SeleniumSession:
    """Manages Selenium WebDriver sessions with advanced configuration."""
    
    def __init__(
        self,
        config: Optional[SeleniumSessionConfig] = None,
        headless: bool = False,
        user_data_dir: Optional[str] = None,
        profile_dir: Optional[str] = None,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        browser_type: Optional[str] = None,  # kept for compatibility, currently Chrome only
    ) -> None:
        """Initialize the Selenium session manager.
        
        Args:
            config: Configuration object for the session
            headless: Run browser in headless mode
            user_data_dir: Directory for user data (profiles, cookies, etc.)
            profile_dir: Specific profile directory to use
            proxy: Proxy server to use (format: host:port or user:pass@host:port)
            user_agent: Custom user agent string
            logger: Logger instance for logging
        """
        self.logger = logger or logging.getLogger(__name__)
        
        # Initialize configuration
        if config is None:
            self.config = SeleniumSessionConfig(
                headless=headless,
                user_data_dir=user_data_dir,
                profile_dir=profile_dir,
                proxy=proxy,
                user_agent=user_agent
            )
        else:
            self.config = config
        
        self.driver: Optional[WebDriver] = None
        self._is_running = False
        self._browser_type = browser_type or "chrome"
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
    
    def start(self) -> WebDriver:
        """Start the WebDriver session."""
        if self._is_running and self.driver is not None:
            return self.driver
            
        try:
            options = self._get_chrome_options()
            
            # Initialize the WebDriver with undetected-chromedriver
            self.driver = uc.Chrome(
                options=options,
                headless=self.config.headless,
                use_subprocess=True,
                version_main=114  # Target Chrome 114 for better compatibility
            )
            
            # Configure timeouts
            self.driver.set_page_load_timeout(self.config.page_load_timeout)
            self.driver.set_script_timeout(self.config.script_timeout)
            self.driver.implicitly_wait(self.config.implicit_wait)
            
            self._is_running = True
            self.logger.info("Selenium WebDriver session started")
            
            return self.driver
            
        except Exception as e:
            self.logger.error(f"Failed to start WebDriver: {str(e)}", exc_info=True)
            self.stop()
            raise
    
    def set_page_load_timeout(self, seconds: int) -> None:
        if self.driver:
            self.driver.set_page_load_timeout(seconds)

    def set_script_timeout(self, seconds: int) -> None:
        if self.driver:
            self.driver.set_script_timeout(seconds)

    def human_pause(self, min_ms: int = 400, max_ms: int = 1200) -> None:
        """Inject human-like jittered pauses to avoid bot-like timing."""
        sleep_for = random.uniform(min_ms, max_ms) / 1000.0
        time.sleep(sleep_for)

    def capture_screenshot(self, path: Optional[str] = None) -> Optional[str]:
        """Capture screenshot if driver is available."""
        if not self.driver:
            return None
        target = path or str(Path(self.config.user_data_dir) / f"shot_{int(time.time())}.png")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            self.driver.save_screenshot(target)
            return target
        except Exception as exc:
            self.logger.warning("Failed to capture screenshot", extra={"error": str(exc), "path": target})
            return None
    
    def _get_chrome_options(self) -> uc.ChromeOptions:
        """Configure Chrome options based on the current configuration."""
        options = uc.ChromeOptions()
        
        # Basic options
        if self.config.headless:
            options.add_argument("--headless=new")
        
        options.add_argument(f"--window-size={self.config.window_width},{self.config.window_height}")
        
        # Performance optimizations
        if self.config.disable_gpu:
            options.add_argument("--disable-gpu")
        if self.config.disable_webgl:
            options.add_argument("--disable-webgl")
        if self.config.disable_extensions:
            options.add_argument("--disable-extensions")
        if self.config.disable_notifications:
            options.add_argument("--disable-notifications")
        if self.config.disable_popup_blocking:
            options.add_argument("--disable-popup-blocking")
        if self.config.no_sandbox:
            options.add_argument("--no-sandbox")
        if self.config.disable_dev_shm_usage:
            options.add_argument("--disable-dev-shm-usage")
        if self.config.disable_infobars:
            options.add_argument("--disable-infobars")
        
        # User data and profiles
        if self.config.user_data_dir:
            options.add_argument(f"--user-data-dir={self.config.user_data_dir}")
        if self.config.profile_dir:
            options.add_argument(f"--profile-directory={self.config.profile_dir}")
        
        # Proxy configuration
        if self.config.proxy:
            proxy_config = f"--proxy-server={self.config.proxy}"
            options.add_argument(proxy_config)
        
        # User agent
        if self.config.user_agent:
            options.add_argument(f"--user-agent={self.config.user_agent}")
        
        # Disable images if needed
        if self.config.disable_images:
            options.add_experimental_option(
                "prefs", {
                    "profile.managed_default_content_settings.images": 2,
                    "profile.default_content_setting_values.images": 2
                }
            )
        
        # Disable JavaScript if needed
        if self.config.disable_javascript:
            options.add_experimental_option(
                "prefs", {
                    "profile.managed_default_content_settings.javascript": 2,
                    "profile.default_content_setting_values.javascript": 2
                }
            )
        
        # Add any additional experimental options
        for key, value in self.config.experimental_options.items():
            options.add_experimental_option(key, value)
        
        return options
    
    def stop(self) -> None:
        """Stop the WebDriver session and clean up resources."""
        if self.driver is not None:
            try:
                self.driver.quit()
                self.logger.info("WebDriver session terminated")
            except Exception as e:
                self.logger.error(f"Error while quitting WebDriver: {str(e)}", exc_info=True)
            finally:
                self.driver = None
                self._is_running = False
    
    def restart(self) -> WebDriver:
        """Restart the WebDriver session."""
        self.stop()
        return self.start()
    
    def screenshot(self, path: Optional[str] = None) -> Optional[str]:
        """Take a screenshot of the current page.
        
        Args:
            path: Path to save the screenshot. If None, a temporary path will be used.
            
        Returns:
            Path to the saved screenshot, or None if failed.
        """
        if not self._is_running or self.driver is None:
            self.logger.warning("Cannot take screenshot: WebDriver not running")
            return None
            
        try:
            if path is None:
                # Create screenshots directory if it doesn't exist
                screenshots_dir = os.path.join(os.getcwd(), 'screenshots')
                os.makedirs(screenshots_dir, exist_ok=True)
                path = os.path.join(
                    screenshots_dir,
                    f'screenshot_{time.strftime("%Y%m%d_%H%M%S")}.png'
                )
            
            # Take screenshot
            self.driver.save_screenshot(path)
            self.logger.info(f"Screenshot saved to {path}")
            return path
            
        except Exception as e:
            self.logger.error(f"Failed to take screenshot: {str(e)}", exc_info=True)
            return None
    
    def wait_for_element(
        self, 
        by: str, 
        selector: str, 
        timeout: Optional[int] = None,
        poll_frequency: float = 0.5
    ) -> WebElement:
        """Wait for an element to be present on the page.
        
        Args:
            by: Locator strategy (e.g., By.ID, By.CSS_SELECTOR)
            selector: Selector string
            timeout: Maximum time to wait in seconds
            poll_frequency: How often to check for the element
            
        Returns:
            The found WebElement
            
        Raises:
            TimeoutException: If element is not found within timeout
        """
        if not self._is_running or self.driver is None:
            raise RuntimeError("WebDriver is not running")
            
        timeout = timeout or self.config.page_load_timeout
        wait = WebDriverWait(self.driver, timeout, poll_frequency=poll_frequency)
        return wait.until(EC.presence_of_element_located((by, selector)))
    
    def wait_for_element_clickable(
        self, 
        by: str, 
        selector: str, 
        timeout: Optional[int] = None,
        poll_frequency: float = 0.5
    ) -> WebElement:
        """Wait for an element to be clickable.
        
        Args:
            by: Locator strategy (e.g., By.ID, By.CSS_SELECTOR)
            selector: Selector string
            timeout: Maximum time to wait in seconds
            poll_frequency: How often to check for the element
            
        Returns:
            The clickable WebElement
            
        Raises:
            TimeoutException: If element is not clickable within timeout
        """
        if not self._is_running or self.driver is None:
            raise RuntimeError("WebDriver is not running")
            
        timeout = timeout or self.config.page_load_timeout
        wait = WebDriverWait(self.driver, timeout, poll_frequency=poll_frequency)
        return wait.until(EC.element_to_be_clickable((by, selector)))
    
    def is_element_present(self, by: str, selector: str, timeout: int = 5) -> bool:
        """Check if an element is present on the page.
        
        Args:
            by: Locator strategy
            selector: Element selector
            timeout: Maximum time to wait in seconds
            
        Returns:
            True if element is present, False otherwise
        """
        try:
            self.wait_for_element(by, selector, timeout=timeout)
            return True
        except TimeoutException:
            return False
    
    def scroll_into_view(self, element: Union[WebElement, str], by: Optional[str] = None) -> None:
        """Scroll an element into view.
        
        Args:
            element: WebElement or selector string
            by: Locator strategy (required if element is a string)
        """
        if not self._is_running or self.driver is None:
            raise RuntimeError("WebDriver is not running")
            
        if isinstance(element, str):
            if by is None:
                raise ValueError("Locator strategy (by) is required when element is a string")
            element = self.wait_for_element(by, element)
            
        self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
    
    def execute_script(self, script: str, *args) -> Any:
        """Execute JavaScript in the browser.
        
        Args:
            script: JavaScript code to execute
            *args: Arguments to pass to the script
            
        Returns:
            The result of the script execution
        """
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        return self.driver.execute_script(script, *args)
    
    def get_cookies(self) -> List[Dict[str, Any]]:
        """Get all cookies from the current session."""
        if self.driver is None:
            return []
        return self.driver.get_cookies()
    
    def add_cookie(self, cookie_dict: Dict[str, Any]) -> None:
        """Add a cookie to the current session."""
        if self.driver is not None:
            self.driver.add_cookie(cookie_dict)
    
    def delete_all_cookies(self) -> None:
        """Delete all cookies from the current session."""
        if self.driver is not None:
            self.driver.delete_all_cookies()
    
    def get_current_url(self) -> str:
        """Get the current page URL."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        return self.driver.current_url
    
    def navigate_to(self, url: str) -> None:
        """Navigate to the specified URL."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.get(url)
    
    def refresh_page(self) -> None:
        """Refresh the current page."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.refresh()
    
    def go_back(self) -> None:
        """Go back to the previous page."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.back()
    
    def go_forward(self) -> None:
        """Go forward to the next page in history."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.forward()
    
    def switch_to_frame(self, frame_reference: Union[str, int, WebElement]) -> None:
        """Switch to the specified frame."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.switch_to.frame(frame_reference)
    
    def switch_to_default_content(self) -> None:
        """Switch back to the default content."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.switch_to.default_content()
    
    def switch_to_window(self, window_handle: str) -> None:
        """Switch to the specified window."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.switch_to.window(window_handle)
    
    def close_current_window(self) -> None:
        """Close the current window."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        self.driver.close()
    
    def get_window_handles(self) -> List[str]:
        """Get all window handles."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        return self.driver.window_handles
    
    def get_current_window_handle(self) -> str:
        """Get the current window handle."""
        if self.driver is None:
            raise RuntimeError("WebDriver is not running")
        return self.driver.current_window_handle


def timestamped_screenshot_path(base_dir: str, prefix: str) -> str:
    """Generate a timestamped screenshot path.
    
    Args:
        base_dir: Base directory to save the screenshot
        prefix: Prefix for the screenshot filename
        
    Returns:
        Full path to the screenshot file
    """
    os.makedirs(base_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(base_dir, f"{prefix}_{timestamp}.png")
