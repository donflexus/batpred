# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Standalone AppDaemon app publishing an ASHP load forecast sensor.

This app fetches hourly weather data from Open-Meteo, applies a simple
weather-driven ASHP model, and publishes the resulting cumulative kWh
forecast in a format Predbat can consume via load_forecast.
"""

from datetime import datetime, timedelta, timezone

import pytz
import requests

try:
	import appdaemon.plugins.hass.hassapi as hass
except ImportError:
	import hass


class ASHPForecast(hass.Hass):
	"""Publish an ASHP forecast sensor for Predbat."""

	def initialize(self):
		"""Read configuration, schedule updates, and publish the first forecast."""
		self.entity_id = self.args.get("entity_id", "sensor.ashp_load_forecast")
		self.latitude = float(self.args["latitude"])
		self.longitude = float(self.args["longitude"])
		self.forecast_days = max(int(self.args.get("forecast_days", 2)), 1)
		self.update_every = max(int(self.args.get("update_every", 60)), 15)
		self.http_timeout = max(int(self.args.get("http_timeout", 30)), 5)
		self.timezone_name = self.args.get("timezone", "UTC")
		self.local_tz = pytz.timezone(self.timezone_name)

		self.base_temperature = float(self.args.get("base_temperature", 12.0))
		self.intercept_kwh = float(self.args.get("intercept_kwh", 0.0))
		self.hdd_slope_kwh_per_deg = float(self.args.get("hdd_slope_kwh_per_deg", 0.0))
		self.wind_slope_kwh_per_ms = float(self.args.get("wind_slope_kwh_per_ms", 0.0))
		self.solar_slope_kwh_per_wm2 = float(self.args.get("solar_slope_kwh_per_wm2", 0.0))
		self.min_kwh = float(self.args.get("min_kwh", 0.0))
		self.max_kwh = float(self.args.get("max_kwh", 10.0))

		period_seconds = self.update_every * 60
		now = datetime.now(self.local_tz)
		midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
		seconds_now = int((now - midnight).total_seconds())
		seconds_offset = seconds_now % period_seconds
		seconds_next = seconds_now + (period_seconds - seconds_offset)
		next_time = midnight + timedelta(seconds=seconds_next)

		self.log(
			"ASHPForecast: entity {} lat {} lon {} update every {} minutes forecast_days {}".format(
				self.entity_id,
				self.latitude,
				self.longitude,
				self.update_every,
				self.forecast_days,
			)
		)

		self.run_every(self.update_forecast, next_time, period_seconds, random_start=0, random_end=0)
		self.update_forecast({})

	def update_forecast(self, cb_args):
		"""Fetch weather, build forecast, and publish the Home Assistant sensor."""
		try:
			now_utc = datetime.now(timezone.utc)
			current_hour = now_utc.replace(minute=0, second=0, microsecond=0)
			hourly_rows = self.fetch_weather_forecast()
			results = {}
			external = []
			hourly_kwh = {}
			cumulative = 0.0

			current_stamp = current_hour.isoformat()
			results[current_stamp] = 0.0
			external.append({"last_updated": current_stamp, "energy": 0.0})
			hourly_kwh[current_stamp] = 0.0

			for row in hourly_rows:
				stamp = row["time"]
				if stamp <= current_hour:
					continue
				forecast_kwh = self.predict_hourly_kwh(row)
				cumulative = round(cumulative + forecast_kwh, 4)
				stamp_text = stamp.isoformat()
				results[stamp_text] = cumulative
				external.append({"last_updated": stamp_text, "energy": cumulative})
				hourly_kwh[stamp_text] = forecast_kwh

			if len(external) <= 1:
				raise ValueError("No forecast rows available after current hour")

			next_hour_stamp = sorted(hourly_kwh.keys())[1]
			next_hour_kwh = hourly_kwh[next_hour_stamp]
			attributes = {
				"friendly_name": "ASHP Load Forecast",
				"unit_of_measurement": "kWh",
				"state_class": "measurement",
				"icon": "mdi:heat-pump-outline",
				"results": results,
				"external": external,
				"hourly_kwh": hourly_kwh,
				"forecast_hours": len(external) - 1,
				"forecast_start": current_stamp,
				"forecast_end": external[-1]["last_updated"],
				"model": {
					"type": "heating_degree_weather",
					"base_temperature": self.base_temperature,
					"intercept_kwh": self.intercept_kwh,
					"hdd_slope_kwh_per_deg": self.hdd_slope_kwh_per_deg,
					"wind_slope_kwh_per_ms": self.wind_slope_kwh_per_ms,
					"solar_slope_kwh_per_wm2": self.solar_slope_kwh_per_wm2,
					"min_kwh": self.min_kwh,
					"max_kwh": self.max_kwh,
				},
				"source": {
					"provider": "open-meteo",
					"latitude": self.latitude,
					"longitude": self.longitude,
					"timezone": "UTC",
				},
				"last_updated": now_utc.isoformat(),
			}

			self.set_state(self.entity_id, state=round(next_hour_kwh, 4), attributes=attributes)
			self.log("ASHPForecast: Published {} hours to {} next hour {} kWh total {} kWh".format(len(external) - 1, self.entity_id, round(next_hour_kwh, 4), round(cumulative, 4)))
		except Exception as err:
			self.log("Error: ASHPForecast failed to update {}".format(err))

	def fetch_weather_forecast(self):
		"""Fetch hourly weather forecast rows from Open-Meteo in UTC."""
		params = {
			"latitude": self.latitude,
			"longitude": self.longitude,
			"hourly": "temperature_2m,wind_speed_10m,shortwave_radiation",
			"forecast_days": self.forecast_days + 1,
			"timezone": "UTC",
		}
		response = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=self.http_timeout)
		response.raise_for_status()
		payload = response.json()
		hourly = payload.get("hourly", {})
		times = hourly.get("time", [])
		temperatures = hourly.get("temperature_2m", [])
		wind_speeds = hourly.get("wind_speed_10m", [])
		solar_radiation = hourly.get("shortwave_radiation", [])

		rows = []
		for time_text, temperature, wind_speed, shortwave in zip(times, temperatures, wind_speeds, solar_radiation):
			stamp = datetime.fromisoformat(time_text)
			if stamp.tzinfo is None:
				stamp = stamp.replace(tzinfo=timezone.utc)
			rows.append({
				"time": stamp,
				"temperature_2m": float(temperature),
				"wind_speed_10m": float(wind_speed),
				"shortwave_radiation": float(shortwave),
			})
		return rows

	def predict_hourly_kwh(self, row):
		"""Predict one hour of ASHP consumption from weather inputs."""
		heating_degree = max(self.base_temperature - row["temperature_2m"], 0.0)
		forecast_kwh = self.intercept_kwh
		forecast_kwh += heating_degree * self.hdd_slope_kwh_per_deg
		forecast_kwh += row["wind_speed_10m"] * self.wind_slope_kwh_per_ms
		forecast_kwh += row["shortwave_radiation"] * self.solar_slope_kwh_per_wm2
		forecast_kwh = max(self.min_kwh, forecast_kwh)
		forecast_kwh = min(self.max_kwh, forecast_kwh)
		return round(forecast_kwh, 4)