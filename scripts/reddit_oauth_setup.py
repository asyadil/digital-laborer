"""Interactive Reddit OAuth setup script."""
import praw
import os
from dotenv import load_dotenv

def setup_reddit_oauth():
    print("=== Reddit OAuth Setup ===\t")
    print("1. Go to https://www.reddit.com/prefs/apps")
    print("2. Click 'create app' or 'create another app'")
    print("3. Select 'script' type")
    print("4. Set redirect uri to: http://localhost:8080")
    print("5. Enter the following details when prompted\n")
    
    client_id = input("Enter client_id: ").strip()
    client_secret = input("Enter client_secret: ").strip()
    username = input("Enter Reddit username: ").strip()
    password = input("Enter Reddit password: ").strip()
    user_agent = f"referral_automation_system/1.0 by /u/{username}"
    
    # Test credentials
    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent
        )
        me = reddit.user.me()
        print(f"\n✅ Authentication successful! Logged in as: {me.name}")
        
        # Save to .env
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
        with open(env_path, 'a') as f:
            f.write(f"\n# Reddit Credentials")
            f.write(f"\nREDDIT_CLIENT_ID={client_id}")
            f.write(f"\nREDDIT_CLIENT_SECRET={client_secret}")
            f.write(f"\nREDDIT_USERNAME={username}")
            f.write(f"\nREDDIT_PASSWORD={password}")
            f.write(f"\nREDDIT_USER_AGENT=\'{user_agent}\'")
        
        print(f"✅ Credentials saved to {env_path}")
        print("\n🚀 Setup complete! You can now run the main application.")
        return True
        
    except Exception as e:
        print(f"\n❌ Authentication failed: {str(e)}")
        print("\nTroubleshooting:")
        print("1. Double-check your credentials")
        print("2. Make sure your Reddit account has verified email")
        print("3. Check if you have 2FA enabled (you'll need to use an app password)")
        print("4. Make sure the app type is set to 'script' in Reddit app settings")
        return False

if __name__ == "__main__":
    load_dotenv()
    setup_reddit_oauth()
