"""Configuration for Loughborough Town Hall (loughboroughtownhall.co.uk) scraper."""

SITE_ID = "loughboroughtownhall"
BASE_URL = "https://www.loughboroughtownhall.co.uk/"

PAGES = [
    (f"{BASE_URL}whats-on/?_sfm_genre=Drama-%2C-Theatre", "Play")
]

COOKIE_BTN_XPATH = "//button[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']"

HEADLESS = True
PAGE_LOAD_TIMEOUT = 60
IFRAME_WAIT_TIMEOUT = 5
SEAT_WAIT_TIMEOUT = 5

DELAY_BETWEEN_SHOWS = (2, 4)
DELAY_BETWEEN_PERFS = (1, 3)
