"""Loughborough Town Hall (loughboroughtownhall.co.uk) extractor."""

import json
import re
import time
from datetime import date

import pandas as pd
from dateutil import parser
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils.base_extractor import BaseExtractor
from utils.logger import setup_logger
from utils.scraping_helpers import (
    accept_cookies,
    convert_to_24hr,
    extract_postcode,
    format_datetime_key,
    get_city_country_uk,
    get_currency_from_price,
    get_scrape_datetime,
    human_delay,
    normalize_country,
    parse_booking_dates,
    standardize_category,
)

from .loughborough_town_hall_config import (
    BASE_URL,
    COOKIE_BTN_XPATH,
    DELAY_BETWEEN_PERFS,
    DELAY_BETWEEN_SHOWS,
    HEADLESS,
    PAGE_LOAD_TIMEOUT,
    PAGES,
    SEAT_WAIT_TIMEOUT,
    SITE_ID,
)

logger = setup_logger(__name__, log_to_file=False)


class LoughboroughtownhallExtractor(BaseExtractor):
    def __init__(self, local_test=False, show_count=None, **kwargs):
        super().__init__(site_id=SITE_ID, **kwargs)
        self.local_test = local_test
        self.show_count = show_count

    # ------------------------------------------------------------------
    # BaseExtractor interface
    # ------------------------------------------------------------------

    def extract(self) -> bytes:
        all_data = []
        venue_details = {"address": None, "city": None, "country": None}
        driver = self.launch_driver(
            headless=HEADLESS, page_load_timeout=PAGE_LOAD_TIMEOUT
        )

        try:
            all_shows = []
            for i, (url, category) in enumerate(PAGES):
                self.custom_logger.info(f"[Listing] {category}: {url}")
                driver.get(url)
                accept_cookies(driver, xpath=COOKIE_BTN_XPATH)
                self._scroll_to_load_all(driver)
                if i == 0:
                    venue_details = self._get_venue_details(driver)
                    self.custom_logger.info(
                        f"  {venue_details['address']} | "
                        f"{venue_details['city']}, {venue_details['country']}"
                    )

                # Navigate back to the listing page ---
                driver.get(url)
                self._scroll_to_load_all(driver)
                # ---------------------------------------------------

                shows = self._extract_event_list(driver, category)
                self.custom_logger.info(f"  → {len(shows)} show(s) found")
                all_shows.extend(shows)

            # Deduplicate by URL — a show listed under both categories should only be scraped once
            seen_urls: set[str] = set()
            deduped: list[dict] = []
            for show in all_shows:
                url = show["event_url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    deduped.append(show)
                else:
                    self.custom_logger.info(
                        f"  Skipping duplicate: {show['title']!r} (already queued)"
                    )
            all_shows = deduped

            if self.show_count:
                all_shows = all_shows[: self.show_count]
                self.custom_logger.info(
                    f"show_count={self.show_count}: limited to {len(all_shows)} show(s)"
                )

            for idx, show in enumerate(all_shows, 1):
                self.custom_logger.info(
                    f"[{idx}/{len(all_shows)}] [{show['category']}] {show['title']!r}"
                )
                try:
                    record = self._scrape_show(driver, show, venue_details)
                    if record:
                        all_data.append(record)
                        self.log_record(record)
                        self._log_show_summary(record)
                except Exception as exc:
                    self.custom_logger.error(f"  ✗ Error: {exc}", exc_info=True)

                human_delay(*DELAY_BETWEEN_SHOWS)

            self.custom_logger.info(f"Extraction complete — {len(all_data)} record(s)")

        finally:
            try:
                driver.quit()
            except Exception:
                pass

        return json.dumps(all_data, default=str).encode("utf-8")

    # ------------------------------------------------------------------
    # Level 2 — Show detail
    # ------------------------------------------------------------------

    def _scrape_show(self, driver, show: dict, venue_details: dict) -> dict | None:
        for attempt in range(1, 4):
            try:
                driver.get(show["event_url"])
                break
            except (TimeoutException, WebDriverException) as exc:
                self.custom_logger.warning(
                    f"  Load attempt {attempt}/3 failed for {show['title']!r}: "
                    f"{type(exc).__name__}"
                )
                if attempt == 3:
                    raise
                time.sleep(3)
        accept_cookies(driver, xpath=COOKIE_BTN_XPATH)
        self._scroll_to_load_all(driver)

        performances = self._extract_performances(driver)

        if not performances:
            self.custom_logger.warning(
                f"  No performances found for '{show['title']}', skipping"
            )
            return None

        seat_pricing, currency, capacity = self._scrape_seat_pricing(
            driver, performances
        )

        venue = performances[0].get("venue") if performances else None

        return {
            "title": show["title"],
            "venue_url": show["event_url"],
            "category": standardize_category(show["category"]),
            "venue": venue,
            "address": venue_details["address"],
            "city": venue_details["city"],
            "country": normalize_country(venue_details["country"]),
            "open_date": show["open_date"],
            "close_date": show["close_date"],
            "booking_start_date": show["open_date"],
            "booking_end_date": show["close_date"],
            "upcoming_performances": [
                {"date": p["date"], "time": p["time"]} for p in performances
            ],
            "capacity": capacity,
            "currency": currency,
            "is_limited_run": None,
            "seat_pricing": seat_pricing,
            "scrape_datetime": get_scrape_datetime(),
        }

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if not df.empty and "is_limited_run" in df.columns:
            df["is_limited_run"] = None
        if not df.empty and "capacity" in df.columns:
            df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").astype(
                "Int64"
            )
        return df

    def _parse(self, raw: bytes) -> pd.DataFrame:
        data = json.loads(raw.decode("utf-8"))
        df = pd.DataFrame(data)
        if not df.empty and "capacity" in df.columns:
            if df["capacity"].notna().any():
                df["capacity"] = df["capacity"].astype(pd.Int64Dtype())
        self.custom_logger.info(f"Parsed {len(df)} record(s)")
        return df

    # ------------------------------------------------------------------
    # Level 1 - Address Details
    # ------------------------------------------------------------------
    def _get_venue_details(self, driver) -> dict:
        """Extract venue address from the contact page footer; returns None values on failure.
        Market Place, Loughborough, Leicestershire, LE11 3EB
        """
        data = {"address": None, "city": None, "country": None}
        try:
            # Match the specific contact paragraph using structural relationship selectors
            # Looking inside standard footer -> columns -> target address string paragraph
            # Market Place, Loughborough, Leicestershire, LE11 3EB
            driver.get(f"{BASE_URL}your-visit/")

            address_p = driver.find_element(
                By.CSS_SELECTOR, "footer#colophon div.column p"
            )
            raw_address = address_p.text.strip()
            data["address"] = raw_address

            address_parts = [part.strip() for part in raw_address.split(",")]

            if len(address_parts) >= 3:
                postcode = extract_postcode(raw_address, region="UK")
                data["city"] = address_parts[1]
                if postcode:
                    _, country = get_city_country_uk(postcode)
                    data["country"] = normalize_country(country) if country else None

        except Exception as e:
            self.custom_logger.warning(f"  Venue extraction failed: {e}")
        return data

    # ------------------------------------------------------------------
    # Level 2 — Listing
    # ------------------------------------------------------------------

    def _extract_event_list(self, driver, category: str) -> list[dict]:
        """
        Parses individual cards inside the main events list holder.
        """
        try:
            WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.events_holder div.event_item")
                )
            )
        except TimeoutException:
            self.custom_logger.warning("  No event found on listing page")
            return []

        shows = []
        shows_cards = driver.find_elements(
            By.CSS_SELECTOR, "div.events_holder div.event_item"
        )

        for item in shows_cards:
            try:
                title_element = item.find_element(By.TAG_NAME, "h4")
                title = title_element.get_attribute("textContent").strip()

                link_element = item.find_element(By.TAG_NAME, "a")
                link = link_element.get_attribute("href")

                # "Tue 8 Sep to Sat 12 Sep 2026 - 2:30 PM | 7:30 PM"
                # "Tue 23 Feb 2027 - 7:30 PM"
                date_element = item.find_element(
                    By.CSS_SELECTOR, ".entry-meta .post_meta"
                )
                date_text = date_element.get_attribute("textContent").strip()

                # Remove times
                date_part = date_text.split(" - ")[0].strip()

                # Convert UK numeric dates (DD/MM/YYYY) to named-month form so
                # parse_booking_dates resolves them unambiguously (dayfirst=True).
                def _uk_numeric_to_named(m):
                    try:
                        return date(
                            int(m.group(3)), int(m.group(2)), int(m.group(1))
                        ).strftime("%d %b %Y")
                    except ValueError:
                        return m.group(0)

                normalised = re.sub(
                    r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _uk_numeric_to_named, date_part
                )
                normalised = normalised.replace(" to ", " - ")
                parsed = parse_booking_dates(normalised)
                open_date = parsed.get("start_date")
                close_date = parsed.get("end_date") or open_date

                # If open is after close (year boundary e.g. Dec->Jan), roll open back one year
                if open_date and close_date and open_date > close_date:
                    od = date.fromisoformat(open_date)
                    open_date = od.replace(year=od.year - 1).isoformat()

                shows.append(
                    {
                        "title": title,
                        "event_url": link,
                        "category": category,
                        "open_date": open_date,
                        "close_date": close_date,
                    }
                )

            except Exception:
                continue
        return shows

    # ------------------------------------------------------------------
    # Level 3 — Performance calendar
    # ------------------------------------------------------------------

    def _extract_performances(self, driver) -> list[dict]:
        """Parses the performance instances row by row from the show details grid."""
        performances = []
        venue = "No venue listed"

        try:
            detail_elements = driver.find_elements(
                By.CSS_SELECTOR, ".event_details h5.detail"
            )
            if len(detail_elements) > 1:
                venue = detail_elements[1].text.strip()
        except Exception as inner_e:
            self.custom_logger.warning(
                f"  Unexpected error reading theatre element: {inner_e}"
            )

        try:
            rows = driver.find_elements(
                By.CSS_SELECTOR, "div.show_details_table div.show_row"
            )
            for row in rows:
                date_element = row.find_element(
                    By.CSS_SELECTOR, ".date_col"
                ).text.strip()
                time_element = row.find_element(
                    By.CSS_SELECTOR, ".time_col"
                ).text.strip()

                # Booking URL token
                book_link_el = row.find_element(By.CSS_SELECTOR, ".book_col a")
                book_link = book_link_el.get_attribute("href")

                perf_date = self._parse_perf_date(date_element).strftime("%Y-%m-%d")
                perf_time = convert_to_24hr(time_element)

                if not perf_date and not perf_time:
                    continue

                performances.append(
                    {
                        "date": perf_date,
                        "time": perf_time,
                        "venue": venue,
                        "booking_url": book_link,
                    }
                )

        except Exception as e:
            self.custom_logger.warning(f"  Error extracting performances: {e}")
        return performances

    # ------------------------------------------------------------------
    # Level 4 — Seat pricing via Spektrix iframe
    # ------------------------------------------------------------------

    def _scrape_seat_pricing(
        self, driver, performances: list[dict]
    ) -> tuple[dict, str | None, int | None]:
        seat_pricing = {}
        currency = None
        max_capacity = None

        # NEW FLAG: Tracks if we hit a technical "no seat map available" or layout error
        encountered_no_seatmap = False

        for i, perf in enumerate(performances, 1):
            key = format_datetime_key(perf["date"], perf["time"])
            if not key:
                continue

            # If there's no booking URL (e.g. sold out), we can't get seat pricing, but we can still record the performance with an empty seat list
            if not perf.get("booking_url"):
                seat_pricing[key] = []
                continue

            self.custom_logger.info(
                f"  [{i}/{len(performances)}] Seats for {perf['date']} {perf['time']}"
            )

            try:
                driver.get(perf["booking_url"])

                iframes = driver.find_elements(By.ID, "SpektrixIFrame")

                if iframes:
                    iframe = iframes[0]

                    driver.switch_to.frame(iframe)

                    WebDriverWait(driver, SEAT_WAIT_TIMEOUT).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, "div.SeatingArea img")
                        )
                    )

                    # Grab all img items inside the seating container that carry the seat attribute layout class strings
                    seats = driver.find_elements(
                        By.CSS_SELECTOR, "div.SeatingArea img[class*='Seat']"
                    )
                    self.custom_logger.info(f" Found {len(seats)} unique seats. ")

                    perf_capacity = len(seats)
                    if max_capacity is None or perf_capacity > max_capacity:
                        max_capacity = perf_capacity

                    seat_list = []
                    for img in seats:
                        tooltip = (
                            img.get_attribute("tooltip")
                            or img.get_attribute("title")
                            or ""
                        )
                        if not tooltip:
                            continue

                        match = re.search(r"([A-Z]+\d+)\s*-\s*[££]?([\d,.]+)", tooltip)
                        if not match:
                            continue

                        if currency is None:
                            currency = get_currency_from_price(tooltip)

                        seat_list.append(
                            {
                                "seat": match.group(1),
                                "ticket_price": float(match.group(2).replace(",", "")),
                            }
                        )

                    seat_pricing[key] = seat_list
                    self.custom_logger.info(f"    {len(seat_list)} seats extracted")

                else:
                    # MISSING SEATMAP: Page loaded but iframe layout isn't there
                    seat_pricing[key] = []
                    encountered_no_seatmap = True  # <--- Flagged
                    self.custom_logger.info(
                        f" Non seat map available for {perf['date']} {perf['time']}"
                    )

            except Exception:
                # LAYOUT ERROR / TIMEOUT
                seat_pricing[key] = []
                encountered_no_seatmap = True  # <--- Flagged
                self.custom_logger.info(
                    f" No seat map / unsupported layout for {perf['date']} {perf['time']}"
                )
                continue

            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

            human_delay(*DELAY_BETWEEN_PERFS)

        # =================================================================================
        # CONDITIONAL CHECK:
        # Only clear to {} if we actually hit "no seatmap" issues AND everything is empty.
        # =================================================================================
        if encountered_no_seatmap and all(
            len(seats) == 0 for seats in seat_pricing.values()
        ):
            self.custom_logger.info(
                " All performances lack a seat map layout. Resetting seat_pricing = {}"
            )
            seat_pricing = {}
        # =================================================================================

        return seat_pricing, currency, max_capacity

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_perf_date(self, text: str) -> date | None:
        try:
            dt = parser.parse(text, dayfirst=True, fuzzy=True)
            if dt.date() < date.today():
                dt = dt.replace(year=dt.year + 1)
            return dt
        except Exception as e:
            self.custom_logger.warning(f"_parse_perf_date failed for '{text}': {e}")
            return None

    def _log_show_summary(self, record: dict) -> None:
        seat_pricing = record.get("seat_pricing") or {}
        perfs = record.get("upcoming_performances") or []
        divider = "  " + "━" * 54
        lines = [
            divider,
            f"  ✓  {record['title']}  [{record['category']}]",
            f"     Venue    : {record['venue']}, {record['city']}, {record['country']}",
            f"     Run      : {record['open_date']} → {record['close_date']}",
            f"     Capacity : {record['capacity']}  |  Currency: {record['currency']}",
            f"     Performances ({len(perfs)}):",
        ]
        for p in perfs:
            key = f"{p['date']} {p['time']}"
            seats = seat_pricing.get(key, [])
            seat_label = (
                f"{len(seats)} seats" if seats else "No seat map available or sold out"
            )
            lines.append(f"       • {key}  →  {seat_label}")
        lines.append(divider)
        self.custom_logger.info("\n".join(lines))

    def _scroll_to_load_all(self, driver) -> None:
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height


def main():
    extractor = LoughboroughtownhallExtractor(
        save_csv_locally=False, csv_incremental_mode=False
    )
    result = extractor.run()
    logger.info("Extraction result: %s", result)


if __name__ == "__main__":
    main()
