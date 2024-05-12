"""Lifecycle of Swiss Meteo."""
import datetime
import logging
import pprint
import time
from typing import Any, cast

from async_timeout import timeout
from hamsclientfork import meteoSwissClient
from hamsclientfork.client import ClientResult
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Config, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from custom_components.meteoswiss.const import (
    CONF_FORECAST_NAME,
    CONF_NAME,
    CONF_POSTCODE,
    CONF_REAL_TIME_NAME,
    CONF_STATION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.WEATHER]
MAX_CONTINUOUS_ERROR_TIME = 60 * 60


async def async_setup(hass: HomeAssistant, config: Config):
    """Setup via old entry in configuration.yaml."""
    _LOGGER.debug("Async setup: meteoswiss")

    conf = config.get(DOMAIN)
    if conf is None:
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=conf
        )
    )
    _LOGGER.debug("END Async setup: meteoswiss")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    hass.data.setdefault(DOMAIN, {})

    _LOGGER.debug("Current configuration: %s", entry.data)
    name = entry.data.get(
        CONF_FORECAST_NAME,
        entry.data.get(
            CONF_REAL_TIME_NAME,
            entry.data.get(CONF_NAME, ""),
        ),
    )
    if not name:
        entry_id = entry.entry_id
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{entry_id}_improperly_configured_{DOMAIN}",
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.ERROR,
            translation_key="improperly_configured",
            translation_placeholders={
                "entry_id": entry_id,
            },
        )
        return False

    interval = datetime.timedelta(
        seconds=entry.data.get(
            CONF_UPDATE_INTERVAL,
            DEFAULT_UPDATE_INTERVAL,
        )
        * 60
    )
    coordinator = MeteoSwissDataUpdateCoordinator(
        hass,
        interval,
        entry.data[CONF_POSTCODE],
        station=entry.data.get(CONF_STATION, None),
        forecast_name=entry.data.get(CONF_FORECAST_NAME, name),
        real_time_name=entry.data.get(CONF_REAL_TIME_NAME, None),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.async_on_unload(entry.add_update_listener(update_listener))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(
        entry,
        PLATFORMS,
    )

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class MeteoSwissClientResult(ClientResult):
    station: str
    post_code: str
    forecast_name: str
    real_time_name: str


class MeteoSwissDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching MeteoSwiss data API."""

    data: MeteoSwissClientResult

    def __init__(
        self,
        hass: HomeAssistant,
        update_interval: datetime.timedelta,
        post_code: int,
        station: str | None,
        forecast_name: str,
        real_time_name: str | None,
    ) -> None:
        """Initialize."""
        self.first_error: float | None = None
        self.error_raised = False
        self.hass = hass
        self.post_code = post_code
        self.station = station
        self.forecast_name = forecast_name
        self.real_time_name = real_time_name
        _LOGGER.debug(
            "Forecast %s will be provided for post code %s every %s",
            forecast_name,
            post_code,
            update_interval,
        )

        if station:
            _LOGGER.debug(
                "Real-time %s will be updated from %s every %s",
                real_time_name,
                station,
                update_interval,
            )

        self.client = meteoSwissClient(
            "%s / %s" % (forecast_name, real_time_name),
            post_code,
            station if station else "NO STATION",
        )
        _LOGGER.debug("Client obtained")

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> MeteoSwissClientResult:
        """Update data via library."""
        try:
            async with timeout(15):
                data = await self.hass.async_add_executor_job(
                    self.client.get_typed_data,
                )
        except Exception as exc:
            raise UpdateFailed(exc) from exc

        _LOGGER.debug("Data obtained:\n%s", pprint.pformat(data))
        if self.station:
            if not data.get("condition"):
                # Oh no.  We could not retrieve the URL.
                # We try 20 times.  If it does not succeed,
                # we will induce a config error.
                _LOGGER.warning(
                    "Station %s provided us with no real-time data",
                    self.station,
                )
                if self.first_error is None:
                    self.first_error = time.time()

                m = MAX_CONTINUOUS_ERROR_TIME
                last_error = time.time() - self.first_error
                if not self.error_raised and last_error > m:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        f"{self.station}_provides_no_data_{DOMAIN}",
                        is_fixable=False,
                        is_persistent=False,
                        severity=IssueSeverity.ERROR,
                        translation_key="station_no_data",
                        translation_placeholders={
                            "station": self.station,
                        },
                    )
                    self.error_raised = True
            else:
                if self.error_raised:
                    ir.async_delete_issue(
                        self.hass, DOMAIN, f"{self.station}_provides_no_data_{DOMAIN}"
                    )
                self.first_error = None
                self.error_raised = False

        data[CONF_STATION] = self.station
        data[CONF_POSTCODE] = self.post_code
        data[CONF_FORECAST_NAME] = self.forecast_name
        data[CONF_REAL_TIME_NAME] = self.real_time_name
        return cast(MeteoSwissClientResult, data)
