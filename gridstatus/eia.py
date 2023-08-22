import concurrent.futures
import json
import os
import re
from urllib.request import urlopen

import pandas as pd
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

import gridstatus
from gridstatus import utils
from gridstatus.gs_logging import log


class EIA:
    BASE_URL = "https://api.eia.gov/v2/"

    def __init__(self, api_key=None):
        """Initialize EIA API object

        Args:
            api_key (str, optional): EIA API key.
                If not provided, will look for EIA_API_KEY environment variable.

        """
        if api_key is None:
            api_key = os.environ.get("EIA_API_KEY")
        self.api_key = api_key

        if api_key is None:
            raise ValueError(
                "API key not provided and EIA_API_KEY \
                not found in environment variables.",
            )
        self.api_key = api_key
        self.session = requests.Session()

    def list_routes(self, route="/"):
        """List all available routes"""
        url = f"{self.BASE_URL}{route}"
        params = {
            "api_key": self.api_key,
        }
        data = self.session.get(url, params=params)
        response = data.json()["response"]
        return response

    def _fetch_page(self, url, headers):
        data = self.session.get(url, headers=headers)
        response = data.json()["response"]
        df = pd.DataFrame(response["data"])
        return df, response["total"]

    def get_dataset(self, dataset, start, end, n_workers=1, verbose=False):
        """Get data from a dataset

        Only supports "electricity/rto/interchange-data" dataset for now.

        Args:
            dataset (str): Dataset path
            start (str or pd.Timestamp): Start date
            end (str or pd.Timestamp): End date
            n_workers (int, optional): Number of
                workers to use for fetching data. Defaults to 1.
            verbose (bool, optional): Whether
                to print progress. Defaults to False.

        Returns:
            pd.DataFrame: Dataframe with data from the dataset

        """
        start = gridstatus.utils._handle_date(start, "UTC")
        start_str = start.strftime("%Y-%m-%dT%H")

        end_str = None
        if end:
            end = gridstatus.utils._handle_date(end, "UTC")
            end_str = end.strftime("%Y-%m-%dT%H")

        url = f"{self.BASE_URL}{dataset}/data/"

        params = {
            "start": start_str,
            "end": end_str,
            "frequency": "hourly",
            "data": [
                "value",
            ],
            "facets": {},
            "offset": 0,
            "length": 5000,
        }

        headers = {
            "X-Api-Key": self.api_key,
            "X-Params": json.dumps(params),
        }

        log(f"Fetching data from {url}", verbose=verbose)
        log(f"Params: {params}", verbose=verbose)
        log(
            f"Concurrent workers: {n_workers}",
            verbose=verbose,
        )

        raw_df, total_records = self._fetch_page(url, headers)

        # Calculate the number of pages
        page_size = 5000
        total_pages = (total_records + page_size - 1) // page_size

        if verbose:
            print(f"Total records: {total_records}")
            print(f"Total pages: {total_pages}")
            print("Fetching data:")

        # Fetch the remaining pages if necessary
        def fetch_page_wrapper(url, headers, page, page_size):
            params = json.loads(headers["X-Params"])
            params["offset"] = page * page_size
            headers["X-Params"] = json.dumps(params)
            page_df, _ = self._fetch_page(url, headers)
            return page_df

        if total_pages > 1:
            pages = range(1, total_pages)
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=n_workers,
            ) as executor:  # noqa
                args = ((url, headers.copy(), page, page_size) for page in pages)
                futures = [executor.submit(fetch_page_wrapper, *arg) for arg in args]

                if verbose:
                    with tqdm(total=total_pages, ncols=80) as progress_bar:
                        # for first page done at beginning
                        progress_bar.update(1)
                        for future, page in zip(
                            concurrent.futures.as_completed(futures),
                            pages,
                        ):  # noqa
                            progress_bar.update(1)

                page_dfs = [future.result() for future in futures]

            raw_df = pd.concat([raw_df, *page_dfs], ignore_index=True)

        df = raw_df.copy()

        if dataset in DATASET_HANDLERS:
            df = DATASET_HANDLERS[dataset](df)

        return df

    def get_daily_spots_and_futures(self):
        """
        Retrieves daily spots and futures for select energy products.

        Includes Wholesale Spot and Retail Petroleum, Natural Gas.
        Prompt-Month Futures, broken on EIA side,
        for Crude, Gasoline, Heating Oil, Natural Gas, Coal, Ethanol.

        They are published daily and not persisted, so this should be run once daily.

        Returns:
            d: dictionary of DataFrames for each table of values."""

        url = "https://www.eia.gov/todayinenergy/prices.php"

        df_petrol = pd.DataFrame(columns=["product", "area", "price", "percent_change"])
        df_ng = pd.DataFrame(
            columns=[
                "region",
                "natural_gas_price",
                "natural_gas_percent_change",
                "electricity_price",
                "electricity_percent_change",
                "spark_spread",
            ],
        )

        def contains_wholesale_petroleum(text):
            return text and "Wholesale Spot Petroleum Prices" in text

        with urlopen(url) as response:
            soup = BeautifulSoup(response, "html.parser")

            close_date = soup.find("b", text=contains_wholesale_petroleum).text

            pattern = r"\b\d{1,2}/\d{1,2}/\d{2}\b"
            close_date = re.findall(pattern=pattern, string=close_date)[0]

            print(type(close_date))
            print(close_date)
            wholesale_petroleum = soup.select_one(
                "table[summary='Spot Petroleum Prices']",
            )

            rowspan_sum = 0
            directions = ["up", "dn", "nc"]
            for s1 in wholesale_petroleum.select("td.s1"):
                text = s1.text
                parent = s1.find_parent("tr").find_parent("table")

                if text == "Commodity Price Index":
                    break
                try:
                    rowspan = int(s1.get("rowspan"))
                    if s1.select("a", class_="lbox"):
                        rowspan -= 1  # down index by one (crack spread)
                        s2 = s1.find_next_sibling("td", class_="s2").text
                        d1 = s1.find_next_sibling("td", class_="d1").text
                        direction = float(
                            s1.find_next_sibling("td", class_=directions).text,
                        )
                        df_petrol.loc[len(df_petrol)] = (text, s2, d1, float(direction))
                    else:
                        for i in range(rowspan_sum, rowspan + rowspan_sum):
                            s2_elements = parent.select("td.s2")
                            d1_elements = parent.select("td.d1")
                            direction_elements = parent.find_all(class_=directions)
                            df_petrol.loc[len(df_petrol)] = (
                                text,
                                s2_elements[i].text,
                                d1_elements[i].text,
                                float(direction_elements[i].text),
                            )

                    rowspan_sum += rowspan
                except TypeError:
                    s2 = s1.find_next_sibling("td", class_="s2").text
                    d1 = s1.find_next_sibling("td", class_="d1").text
                    direction = float(
                        s1.find_next_sibling("td", class_=directions).text,
                    )
                    df_petrol.loc[len(df_petrol)] = (text, s2, d1, float(direction))

            natural_gas_spots = soup.select_one(
                "table[summary='Spot Natural Gas and Electric Power Prices']",
            )

            for s1 in natural_gas_spots.select("td.s1"):
                price_siblings = s1.find_next_siblings("td", class_="d1")
                direction_siblings = s1.find_next_siblings("td", class_=directions)
                df_ng.loc[len(df_ng)] = (
                    s1.text,
                    price_siblings[0].text,
                    float(direction_siblings[0].text),
                    price_siblings[1].text,
                    float(direction_siblings[1].text),
                    price_siblings[2].text,
                )

        df_ng["date"] = pd.to_datetime(close_date)
        df_petrol["date"] = pd.to_datetime(close_date)

        df_ng = utils.move_cols_to_front(df_ng, cols_to_move=["date"])
        df_petrol = utils.move_cols_to_front(df_petrol, cols_to_move=["date"])

        d = {
            "df_petrol": df_petrol,
            "df_ng": df_ng,
        }

        return d

    def get_coal_spots(self):
        """
        Retrieve weekly coal commodity spot prices.
        Spot prices are proprietary of S&P Global.
        Historicals cannot be published.
        """

        url = [
            "https://www.eia.gov/coal/markets",
            "https://www.eia.gov/coal/markets/#tabs-prices-2",
        ]

        url = "https://www.eia.gov/coal/markets/coal_markets_json.php"

        with requests.get(url) as r:
            json = r.json()

        print(json.keys())
        keys = json["data"][0].keys()

        for key in keys:
            print(key)
        # with urlopen(url[0]) as response:

        #     soup = BeautifulSoup(response, 'html.parser')

        #     #print(soup)

        #     dpst_dates = soup.find("tr", id="dpst_dates")

        #     #print(dpst_dates)

        #     dpst_data = soup.find("tbody", id="dpst_data")

        #     print(dpst_data)
        #     mmbtu_data = soup.find("tbody", id="mmbtu_dates")
        #     print(mmbtu_data)


def _handle_time(df, frequency="1h"):
    df.insert(0, "Interval End", pd.to_datetime(df["period"], utc=True))
    df.insert(0, "Interval Start", df["Interval End"] - pd.Timedelta(frequency))
    df = df.drop("period", axis=1)
    return df


def _handle_region_data(df):
    df = _handle_time(df, frequency="1h")

    df = df.rename(
        {
            "value": "MW",
            "respondent": "Respondent",
            "respondent-name": "Respondent Name",
            "type": "Type",
        },
        axis=1,
    )

    # ['TI', 'NG', 'DF', 'D']
    df["Type"] = df["Type"].map(
        {
            "D": "Load",
            "TI": "Total Interchange",
            "NG": "Net Generation",
            "DF": "Load Forecast",
        },
    )

    df["MW"] = df["MW"].astype("Int64")

    # pivot on type
    df = df.pivot_table(
        index=["Interval Start", "Interval End", "Respondent", "Respondent Name"],
        columns="Type",
        values="MW",
    ).reset_index()

    df.columns.name = None

    # fix after pivot
    for col in ["Load", "Net Generation", "Load Forecast", "Total Interchange"]:
        df[col] = df[col].astype("Int64")

    return df


def _handle_rto_interchange(df):
    """electricity/rto/interchange-data"""
    df = _handle_time(df, frequency="1h")
    df = df.rename(
        {
            "value": "MW",
            "fromba": "From BA",
            "toba": "To BA",
            "fromba-name": "From BA Name",
            "toba-name": "To BA Name",
        },
        axis=1,
    )
    df = df[
        [
            "Interval Start",
            "Interval End",
            "From BA",
            "From BA Name",
            "To BA",
            "To BA Name",
            "MW",
        ]
    ]

    df = df.sort_values(["Interval Start", "From BA"])

    return df


DATASET_HANDLERS = {
    "electricity/rto/interchange-data": _handle_rto_interchange,
    "electricity/rto/region-data": _handle_region_data,
}

# docs
# https://www.eia.gov/opendata/documentation.php
