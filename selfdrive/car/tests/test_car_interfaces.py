import os
import math
import hypothesis.strategies as st
from hypothesis import Phase, given, settings
from parameterized import parameterized
from collections.abc import Callable
from typing import Any

from cereal import car
from opendbc.car import DT_CTRL, gen_empty_fingerprint, structs
from opendbc.car.car_helpers import interfaces
from opendbc.car.structs import CarParams
from opendbc.car.fingerprints import all_known_cars
from opendbc.car.fw_versions import FW_VERSIONS, FW_QUERY_CONFIGS
from opendbc.car.mock.values import CAR as MOCK
from openpilot.selfdrive.controls.lib.latcontrol_angle import LatControlAngle
from openpilot.selfdrive.controls.lib.latcontrol_pid import LatControlPID
from openpilot.selfdrive.controls.lib.latcontrol_torque import LatControlTorque
from openpilot.selfdrive.controls.lib.longcontrol import LongControl
from openpilot.selfdrive.test.fuzzy_generation import FuzzyGenerator

DrawType = Callable[[st.SearchStrategy], Any]

ALL_ECUS = {ecu for ecus in FW_VERSIONS.values() for ecu in ecus.keys()}
ALL_ECUS |= {ecu for config in FW_QUERY_CONFIGS.values() for ecu in config.extra_ecus}

ALL_REQUESTS = {tuple(r.request) for config in FW_QUERY_CONFIGS.values() for r in config.requests}

MAX_EXAMPLES = int(os.environ.get('MAX_EXAMPLES', '20'))

def get_fuzzy_car_interface_args(draw: DrawType, params_strategy: st.SearchStrategy) -> dict:
  params: dict = draw(params_strategy)
  params['car_fw'] = [structs.CarParams.CarFw(ecu=fw[0], address=fw[1], subAddress=fw[2] or 0,
                                              request=draw(st.sampled_from(sorted(ALL_REQUESTS))))
                      for fw in params['car_fw']]
  return params

def get_global_params_strategy() -> st.SearchStrategy:
  # Fuzzy CAN fingerprints and FW versions to test more states of the CarInterface
  fingerprint_strategy = st.fixed_dictionaries({key: st.dictionaries(st.integers(min_value=0, max_value=0x800),
                                                                     st.integers(min_value=0, max_value=64)) for key in
                                                gen_empty_fingerprint()})

  # only pick from possible ecus to reduce search space
  car_fw_strategy = st.lists(st.sampled_from(sorted(ALL_ECUS)))

  params_strategy = st.fixed_dictionaries({
    'fingerprints': fingerprint_strategy,
    'car_fw': car_fw_strategy,
    'experimental_long': st.booleans(),
  })

  return params_strategy

GLOBAL_STRATEGY = get_global_params_strategy()

class TestCarInterfaces:
  # FIXME: Due to the lists used in carParams, Phase.target is very slow and will cause
  #  many generated examples to overrun when max_examples > ~20, don't use it
  @parameterized.expand([(car,) for car in sorted(all_known_cars())] + [MOCK.MOCK])
  @settings(max_examples=MAX_EXAMPLES, deadline=None,
            phases=(Phase.reuse, Phase.generate, Phase.shrink))
  @given(data=st.data())
  def test_car_interfaces(self, car_name, data):
    CarInterface, CarController, CarState, RadarInterface = interfaces[car_name]

    args = get_fuzzy_car_interface_args(data.draw, GLOBAL_STRATEGY)

    car_params = CarInterface.get_params(car_name, args['fingerprints'], args['car_fw'],
                                         experimental_long=args['experimental_long'], docs=False)
    car_params = car_params.as_reader()
    car_interface = CarInterface(car_params, CarController, CarState)
    assert car_params
    assert car_interface

    assert car_params.mass > 1
    assert car_params.wheelbase > 0
    # centerToFront is center of gravity to front wheels, assert a reasonable range
    assert car_params.wheelbase * 0.3 < car_params.centerToFront < car_params.wheelbase * 0.7
    assert car_params.maxLateralAccel > 0

    # Longitudinal sanity checks
    assert len(car_params.longitudinalTuning.kpV) == len(car_params.longitudinalTuning.kpBP)
    assert len(car_params.longitudinalTuning.kiV) == len(car_params.longitudinalTuning.kiBP)

    # Lateral sanity checks
    if car_params.steerControlType != CarParams.SteerControlType.angle:
      tune = car_params.lateralTuning
      if tune.which() == 'pid':
        if car_name != MOCK.MOCK:
          assert not math.isnan(tune.pid.kf) and tune.pid.kf > 0
          assert len(tune.pid.kpV) > 0 and len(tune.pid.kpV) == len(tune.pid.kpBP)
          assert len(tune.pid.kiV) > 0 and len(tune.pid.kiV) == len(tune.pid.kiBP)

      elif tune.which() == 'torque':
        assert not math.isnan(tune.torque.kf) and tune.torque.kf > 0
        assert not math.isnan(tune.torque.friction) and tune.torque.friction > 0

    cc_msg = FuzzyGenerator.get_random_msg(data.draw, car.CarControl, real_floats=True)
    # Run car interface
    now_nanos = 0
    CC = car.CarControl.new_message(**cc_msg)
    CC = CC.as_reader()
    for _ in range(4):
      car_interface.update([])
      car_interface.apply(CC, now_nanos)
      now_nanos += DT_CTRL * 1e9  # 10 ms

    CC = car.CarControl.new_message(**cc_msg)
    CC.enabled = True
    CC = CC.as_reader()
    for _ in range(4):
      car_interface.update([])
      car_interface.apply(CC, now_nanos)
      now_nanos += DT_CTRL * 1e9  # 10ms

    # Test controller initialization
    # TODO: wait until card refactor is merged to run controller a few times,
    #  hypothesis also slows down significantly with just one more message draw
    LongControl(car_params)
    if car_params.steerControlType == CarParams.SteerControlType.angle:
      LatControlAngle(car_params, car_interface)
    elif car_params.lateralTuning.which() == 'pid':
      LatControlPID(car_params, car_interface)
    elif car_params.lateralTuning.which() == 'torque':
      LatControlTorque(car_params, car_interface)
