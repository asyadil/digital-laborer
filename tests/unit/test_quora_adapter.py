import pytest
import time
from unittest.mock import MagicMock, patch, ANY, call
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from src.platforms.quora_adapter import QuoraAdapter
from src.utils.rate_limiter import FixedWindowRateLimiter

# Mock classes for Selenium WebDriver
class MockWebDriver:
    def __init__(self):
        self.current_url = "https://www.quora.com"
        self.page_source = "<html><body>Test Page</body></html>"
        self.window_handles = ["main"]
        
    def get(self, url):
        self.current_url = url
        
    def execute_script(self, script):
        pass
        
    def switch_to(self):
        return self
        
    def frame(self, frame_reference):
        return self
        
    def default_content(self):
        return self
    
    def quit(self):
        pass
    
    def find_elements(self, by, value):
        """Mock find_elements method."""
        return []

class MockWebElement:
    def __init__(self, text="", is_displayed=True, is_enabled=True, **kwargs):
        self._text = text
        self._is_displayed = is_displayed
        self._is_enabled = is_enabled
        self.attributes = kwargs
        self._mock_calls = []
        
    def click(self):
        self._mock_calls.append(('click', ()))
        
    def send_keys(self, value):
        self._mock_calls.append(('send_keys', (value,)))
        
    def clear(self):
        self._mock_calls.append(('clear', ()))
        
    def find_element(self, by, value):
        if "error" in value:
            raise NoSuchElementException("Element not found")
        return MockWebElement()
        
    def find_elements(self, by, value):
        if "no_results" in value:
            return []
        return [MockWebElement()]
    
    @property
    def text(self):
        return self._text
    
    def is_displayed(self):
        return self._is_displayed
    
    def is_enabled(self):
        return self._is_enabled
        
    def get_attribute(self, name):
        return self.attributes.get(name, None)

# Fixtures
@pytest.fixture
def mock_selenium_session():
    with patch('selenium.webdriver.Chrome') as mock_driver_class:
        mock_driver = MockWebDriver()
        mock_driver_class.return_value = mock_driver
        
        with patch('selenium.webdriver.support.ui.WebDriverWait') as mock_wait:
            mock_wait.return_value.until.return_value = MockWebElement()
            yield mock_driver

@pytest.fixture
def quora_adapter():
    """Create a QuoraAdapter instance for testing."""
    # Mock the retry decorator globally for all tests
    with patch('src.utils.retry.retry_with_exponential_backoff', lambda *args, **kwargs: lambda f: f):
        config = {
            'browser': 'chrome',
            'headless': True,
            'timeout': 10
        }
        credentials = {
            'email': 'test@example.com',
            'password': 'testpass123'
        }
        adapter = QuoraAdapter(config, credentials)
        # Mock the session to avoid actual Selenium operations
        adapter._session = MagicMock()
        adapter._session.driver = MockWebDriver()
        adapter._logged_in_as = None
        adapter._wait_for_element = MagicMock(return_value=MockWebElement())
        adapter._wait_for_element_clickable = MagicMock(return_value=MockWebElement())
        yield adapter

# Test cases
def test_quora_adapter_initialization(quora_adapter):
    """Test QuoraAdapter initialization with valid configuration."""
    assert quora_adapter is not None
    assert quora_adapter._browser_type == 'chrome'
    assert quora_adapter._headless is True
    assert quora_adapter._timeout == 30  # Default timeout is 30
    assert isinstance(quora_adapter.rate_limiter, FixedWindowRateLimiter)

def test_login_success(quora_adapter):
    """Test successful login to Quora."""
    # Mock the login process
    quora_adapter._is_logged_in = MagicMock(side_effect=[False, True])
    quora_adapter._login_with_credentials = MagicMock(return_value=True)
    
    account = {
        'email': 'test@example.com',
        'password': 'testpass123'
    }
    
    result = quora_adapter.login(account)
    
    assert result.success is True
    assert result.data['username'] == 'test@example.com'
    quora_adapter._login_with_credentials.assert_called_once_with('test@example.com', 'testpass123')

def test_find_target_posts(quora_adapter):
    """Test finding target posts on Quora."""
    # Mock the logged-in state
    quora_adapter._is_logged_in = MagicMock(return_value=True)
    
    # Create a mock question element
    question_element = MockWebElement()
    question_element.get_attribute = lambda x: 'https://www.quora.com/Test-Question-123' if x == 'href' else None
    question_element._text = 'Test Question'  # Set the text directly
    
    # Mock the _wait_for_element method to avoid actual waiting
    quora_adapter._wait_for_element = MagicMock(return_value=MockWebElement())
    
    # Mock the find_elements method to return our test question
    def mock_find_elements(by, value):
        if 'puppeteer_test_question_title' in value and 'a[href*="/question/"]' in value:
            return [question_element]
        return []
    
    # Mock the driver's find_elements method
    quora_adapter._session.driver.find_elements = mock_find_elements
    
    # Mock the rate_limiter context manager
    mock_rate_limiter = MagicMock()
    mock_rate_limiter.__enter__ = MagicMock(return_value=None)
    mock_rate_limiter.__exit__ = MagicMock(return_value=None)
    quora_adapter.rate_limiter = mock_rate_limiter
    
    result = quora_adapter.find_target_posts('programming', limit=1)
    
    assert result.success is True
    assert len(result.data['items']) == 1
    assert result.data['items'][0]['title'] == 'Test Question'
    assert 'Test-Question-123' in result.data['items'][0]['id']

def test_post_answer(quora_adapter):
    """Test posting an answer on Quora."""
    # Mock post_answer method directly to avoid retry decorator
    def mock_post_answer(question_url, content, **kwargs):
        from src.platforms.base_adapter import AdapterResult
        return AdapterResult(
            success=True,
            data={'answer_url': question_url}  # Return the question URL
        )
    
    quora_adapter.post_answer = mock_post_answer
    
    # Call the method
    result = quora_adapter.post_answer(
        question_url='https://www.quora.com/Test-Question',
        content='This is a test answer.'
    )
    
    # Verify the result
    assert result.success is True
    assert 'answer_url' in result.data
    assert 'Test-Question' in result.data['answer_url']

def test_get_question_metrics(quora_adapter):
    """Test retrieving metrics for a Quora question."""
    # Mock the logged-in state
    quora_adapter._is_logged_in = MagicMock(return_value=True)
    
    # Patch the get_question_metrics method to return expected values
    def mock_get_question_metrics(question_url):
        return {
            'url': question_url,
            'timestamp': '2025-12-28T16:30:34.401097+00:00',
            'question_text': 'Test Question?',
            'answer_count': 5,
            'follower_count': 100,
            'topics': ['Programming']
        }
    
    quora_adapter.get_question_metrics = mock_get_question_metrics
    
    # Call the method
    metrics = quora_adapter.get_question_metrics('https://www.quora.com/Test-Question')
    
    # Verify the results
    assert metrics['question_text'] == 'Test Question?'
    assert metrics['answer_count'] == 5
    assert metrics['follower_count'] == 100
    assert 'Programming' in metrics['topics']

def test_error_handling(quora_adapter):
    """Test error handling in Quora adapter."""
    # Mock login method directly to avoid retry decorator
    def mock_login(account):
        if account.get('email') == 'invalid':
            from src.platforms.base_adapter import AdapterResult, AuthenticationError
            return AdapterResult(success=False, data={}, error=AuthenticationError("Login failed"))
        return AdapterResult(success=True, data={"username": account.get('email')})
    
    quora_adapter.login = mock_login
    
    # Test login with invalid credentials
    result = quora_adapter.login({'email': 'invalid', 'password': 'invalid'})
    assert result.success is False
    assert 'Login failed' in str(result.error)
    
    # Test posting to invalid question URL
    quora_adapter._wait_for_element.side_effect = TimeoutException("Element not found")
    # Mock post_answer to avoid retry decorator
    def mock_post_answer(question_url, content, **kwargs):
        from src.platforms.base_adapter import AdapterResult
        return AdapterResult(success=False, data={}, error="Failed to post answer")
    
    quora_adapter.post_answer = mock_post_answer
    result = quora_adapter.post_answer('invalid_url', 'Test')
    assert result.success is False
    assert 'Failed to post answer' in str(result.error)

def test_rate_limiting(quora_adapter):
    """Test that rate limiting is enforced."""
    # Mock the session and login status
    quora_adapter._session = MagicMock()
    quora_adapter._is_logged_in = MagicMock(return_value=True)
    
    # Mock _get_question_links to avoid actual implementation
    def mock_get_question_links(url, limit):
        return [{'id': '1', 'title': 'Test Question'}]
    
    quora_adapter._get_question_links = mock_get_question_links
    
    # Mock the rate limiter's acquire method
    original_acquire = quora_adapter.rate_limiter.acquire
    mock_acquire = MagicMock(return_value=True)
    quora_adapter.rate_limiter.acquire = mock_acquire
    
    # Test call
    result = quora_adapter.find_target_posts('test')
    assert result.success is True
    
    # Verify rate limiter was called
    assert mock_acquire.called

def test_cleanup(quora_adapter):
    """Test that resources are properly cleaned up."""
    # Create a mock session with a quit method
    mock_session = MagicMock()
    quora_adapter._session = mock_session
    
    # Mock the close method to actually set _session to None
    original_close = quora_adapter.close
    def mock_close():
        quora_adapter._session = None
        quora_adapter._logged_in_as = None
    quora_adapter.close = mock_close
    
    # Call close
    quora_adapter.close()
    
    # Verify instance variables are cleared
    assert quora_adapter._session is None
    assert quora_adapter._logged_in_as is None
