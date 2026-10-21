"""Start Hive Session."""

from aiohttp import ClientSession

from .devices.heating import Climate
from .devices.hotwater import WaterHeater
from .devices.hub import HiveHub
from .devices.light import Light
from .devices.plug import Switch
from .devices.sensor import Sensor
from .session import HiveSession


class Hive(HiveSession):
    """Hive Class.

    Args:
        HiveSession (object): Interact with Hive Account
    """

    def __init__(
        self,
        websession: ClientSession | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        """Generate a Hive session.

        Args:
            websession (Optional[ClientSession], optional): Websession for API calls.
                Defaults to None.
            username (str, optional): This is the Hive username used for login. Defaults to None.
            password (str, optional): This is the Hive password used for login. Defaults to None.
        """
        super().__init__(username, password, websession)
        self.session = self
        self.heating = Climate(self.session)
        self.hotwater = WaterHeater(self.session)
        self.hub = HiveHub(self.session)
        self.light = Light(self.session)
        self.switch = Switch(self.session)
        self.sensor = Sensor(self.session)
