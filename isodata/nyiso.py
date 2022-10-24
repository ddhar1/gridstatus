import io
from zipfile import ZipFile

import pandas as pd
import pytz
import requests

import isodata
from isodata import utils
from isodata.base import FuelMix, GridStatus, ISOBase, Markets
from isodata.decorators import support_date_range


class NYISO(ISOBase):
    name = "New York ISO"
    iso_id = "nyiso"
    default_timezone = "US/Eastern"
    markets = [Markets.REAL_TIME_5_MIN, Markets.DAY_AHEAD_5_MIN]
    status_homepage = "https://www.nyiso.com/system-conditions"

    def get_latest_status(self):
        latest = self._latest_from_today(self.get_status_today)

        status = GridStatus(
            time=latest["time"],
            status=latest["status"],
            reserves=None,
            iso=self,
            notes=latest["notes"],
        )
        return status

    def get_status_today(self):
        """Get status event for today"""
        d = self._today_from_historical(self.get_historical_status)
        return d

    @support_date_range(frequency="MS")
    def get_historical_status(self, date, end=None):
        """Get status event for a date"""
        status_df = self._download_nyiso_archive(
            date=date,
            end=end,
            dataset_name="RealTimeEvents",
        )

        status_df = status_df.rename(
            columns={"Message": "Status"},
        )

        def _parse_status(row):
            STATE_CHANGE = "**State Change. System now operating in "

            row["Notes"] = None
            if row["Status"] == "Start of day system state is NORMAL":
                row["Notes"] = [row["Status"]]
                row["Status"] = "Normal"
            elif STATE_CHANGE in row["Status"]:
                row["Notes"] = [row["Status"]]

                row["Status"] = row["Status"][
                    row["Status"].index(STATE_CHANGE)
                    + len(STATE_CHANGE) : -len(" state.**")
                ].capitalize()

            return row

        status_df = status_df.apply(_parse_status, axis=1)
        status_df = status_df[["Time", "Status", "Notes"]]
        return status_df

    def get_latest_fuel_mix(self):
        # note: this is simlar datastructure to pjm
        url = "https://www.nyiso.com/o/oasis-rest/oasis/currentfuel/line-current"
        data = self._get_json(url)
        mix_df = pd.DataFrame(data["data"])
        time_str = mix_df["timeStamp"].max()
        time = pd.Timestamp(time_str)
        mix_df = mix_df[mix_df["timeStamp"] == time_str].set_index("fuelCategory")[
            "genMWh"
        ]
        mix_dict = mix_df.to_dict()
        return FuelMix(time=time, mix=mix_dict, iso=self.name)

    def get_fuel_mix_today(self):
        "Get fuel mix for today in 5 minute intervals"
        return self._today_from_historical(self.get_historical_fuel_mix)

    @support_date_range(frequency="MS")
    def get_historical_fuel_mix(self, date, end=None):
        mix_df = self._download_nyiso_archive(
            date=date,
            end=end,
            dataset_name="rtfuelmix",
        )
        mix_df = mix_df.pivot_table(
            index="Time",
            columns="Fuel Category",
            values="Gen MW",
            aggfunc="first",
        ).reset_index()
        return mix_df

    def get_latest_demand(self):
        return self._latest_from_today(self.get_demand_today)

    def get_demand_today(self):
        "Get demand for today in 5 minute intervals"
        d = self._today_from_historical(self.get_historical_demand)
        return d

    @support_date_range(frequency="MS")
    def get_historical_demand(self, date, end=None):
        """Returns demand at a previous date in 5 minute intervals"""
        data = self._download_nyiso_archive(
            date=date,
            end=end,
            dataset_name="pal",
        )

        # drop NA loads
        data = data.dropna(subset=["Load"])

        # TODO demand by zone
        demand = data.groupby("Time")["Load"].sum().reset_index()

        demand = demand.rename(
            columns={"Load": "Demand"},
        )

        return demand

    def get_latest_supply(self):
        """Returns most recent data point for supply in MW

        Updates every 5 minutes
        """
        return self._latest_supply_from_fuel_mix()

    def get_supply_today(self):
        "Get supply for today in 5 minute intervals"
        return self._today_from_historical(self.get_historical_supply)

    def get_historical_supply(self, date):
        """Returns supply at a previous date in 5 minute intervals"""
        return self._supply_from_fuel_mix(date)

    def get_forecast_today(self):
        """Get load forecast for today in 1 hour intervals"""
        d = self._today_from_historical(self.get_historical_forecast)
        return d

    @support_date_range(frequency="MS")
    def get_historical_forecast(self, date, end=None):
        """Get load forecast for a previous date in 1 hour intervals"""
        date = utils._handle_date(date, self.default_timezone)

        # todo optimize this to accept a date range
        data = self._download_nyiso_archive(
            date,
            end=end,
            dataset_name="isolf",
        )

        data = data[["File Date", "Time", "NYISO"]].rename(
            columns={
                "File Date": "Forecast Time",
                "NYISO": "Load Forecast",
                "Time": "Time",
            },
        )

        return data

    def get_latest_lmp(self, market: str, locations: list = None):
        return self._latest_lmp_from_today(market=market, locations=locations)

    def get_lmp_today(self, market: str, locations: list = None):
        "Get lmp for today"
        return self._today_from_historical(
            self.get_historical_lmp,
            market=market,
            locations=locations,
        )

    @support_date_range(frequency="MS")
    def get_historical_lmp(
        self,
        date,
        end=None,
        market: str = None,
        locations: list = None,
    ):
        """
        Supported Markets: REAL_TIME_5_MIN, DAY_AHEAD_5_MIN
        """
        # todo support generator and zone
        if locations is None:
            locations = "ALL"

        assert market is not None, "market must be specified"
        market = Markets(market)
        if market == Markets.REAL_TIME_5_MIN:
            marketname = "realtime"
            filename = marketname + "_zone"
        elif market == Markets.DAY_AHEAD_5_MIN:
            marketname = "damlbmp"
            filename = marketname + "_zone"
        else:
            raise RuntimeError("LMP Market is not supported")

        df = self._download_nyiso_archive(
            date=date,
            end=end,
            dataset_name=marketname,
            filename=filename,
        )

        columns = {
            "Name": "Location",
            "LBMP ($/MWHr)": "LMP",
            "Marginal Cost Losses ($/MWHr)": "Loss",
            "Marginal Cost Congestion ($/MWHr)": "Congestion",
        }

        df = df.rename(columns=columns)

        df["Energy"] = df["LMP"] - (df["Loss"] - df["Congestion"])
        df["Market"] = market.value
        df["Location Type"] = "Zone"

        df = df[
            [
                "Time",
                "Market",
                "Location",
                "Location Type",
                "LMP",
                "Energy",
                "Congestion",
                "Loss",
            ]
        ]

        df = utils.filter_lmp_locations(df, locations)

        return df

    def _download_nyiso_archive(self, date, end=None, dataset_name=None, filename=None):

        if filename is None:
            filename = dataset_name

        date = isodata.utils._handle_date(date)
        month = date.strftime("%Y%m01")
        day = date.strftime("%Y%m%d")

        csv_filename = f"{day}{filename}.csv"
        csv_url = f"http://mis.nyiso.com/public/csv/{dataset_name}/{csv_filename}"
        zip_url = (
            f"http://mis.nyiso.com/public/csv/{dataset_name}/{month}{filename}_csv.zip"
        )

        # the last 7 days of file are hosted directly as csv
        if end is None and date > pd.Timestamp.now(
            tz=self.default_timezone,
        ).normalize() - pd.DateOffset(days=7):
            df = pd.read_csv(csv_url)
            df = _handle_time(df)
        else:
            r = requests.get(zip_url)
            z = ZipFile(io.BytesIO(r.content))

            all_dfs = []
            if end is None:
                date_range = [date]
            else:
                try:
                    date_range = pd.date_range(
                        date,
                        end,
                        freq="1D",
                        inclusive="left",
                    )
                except TypeError:
                    date_range = pd.date_range(
                        date,
                        end,
                        freq="1D",
                        closed="left",
                    )

            for d in date_range:
                d = isodata.utils._handle_date(d)
                month = d.strftime("%Y%m01")
                day = d.strftime("%Y%m%d")

                csv_filename = f"{day}{filename}.csv"
                df = pd.read_csv(z.open(csv_filename))
                df["File Date"] = d.normalize()

                df = _handle_time(df)
                all_dfs.append(df)

            df = pd.concat(all_dfs)

        return df


def _handle_time(df):
    if "Time Stamp" in df.columns:
        time_stamp_col = "Time Stamp"
    elif "Timestamp" in df.columns:
        time_stamp_col = "Timestamp"

    def time_to_datetime(s, dst="infer"):
        return pd.to_datetime(s).dt.tz_localize(
            NYISO.default_timezone,
            ambiguous=dst,
        )

    if "Time Zone" in df.columns:
        dst = df["Time Zone"] == "EDT"
        df[time_stamp_col] = time_to_datetime(
            df[time_stamp_col],
            dst,
        )

    elif "Name" in df.columns:
        # once we group by name, the time series for each group is no longer ambiguous
        df[time_stamp_col] = df.groupby("Name")[time_stamp_col].apply(
            time_to_datetime,
            "infer",
        )
    else:
        df[time_stamp_col] = time_to_datetime(
            df[time_stamp_col],
            "infer",
        )

    df = df.rename(columns={time_stamp_col: "Time"})

    return df


"""
pricing data

https://www.nyiso.com/en/energy-market-operational-data
"""
