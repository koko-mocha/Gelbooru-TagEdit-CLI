# Gelbooru Bulk Tag & Metadata Manager CLI

A simple CLI application for frequent Gelbooru taggers and Gardeners of Gelbooru via official Gelbooru's API. Runs in the background within the customizable usage limits.

Built using Python 3.15.

## Setup & Configuration

1. Clone or download this repo.
2. Get dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the app:
   ```bash
   python main.py
   ```
On first-time setup, you should choose `Settings` and configure your Gelbooru credentials. Gelbooru API can be found in website's options. Example: `&api_key=YOUR_API_KEY&user_id=YOUR_USER_ID`

### Cookie Header
Required for authenticated post editing through Gelbooru's endpoint.

1. Be logged-in to Gelbooru. Open devtools
2. Go to network tab and refresh the page.
3. Select a request to gelbooru.com, example `index.php`.
4. Under Headers → Request Headers, find Cookie, and copy its entire value and paste it when asked for Cookie configuration.

Obviously, do not share those credentials with anyone else.