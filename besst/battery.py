from dataclasses import dataclass


@dataclass(frozen=True)
class Battery:
    power_mw: float = 100.0
    capacity_mwh: float = 200.0
    efficiency: float = 0.9  # round trip, applied on discharge

    @property
    def step_mwh(self) -> float:
        """Energy moved in one 5-minute interval at full power."""
        return self.power_mw * 5 / 60
