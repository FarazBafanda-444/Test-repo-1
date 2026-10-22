class Car:

  def __init__(self):
    self.running = False
    self.speed = 0
    self.gear = "N"  # 'R', 'N', 1 to 6

  def start(self):
    if not self.running:
      self.running = True
      print("Engine started.")
    else:
      print("Already running.")

  def stop(self):
    if self.speed == 0:
      self.running = False
      print("Engine stopped.")
    else:
      print("Come to a complete stop before turning off the engine.")

  def change_gear(self, target_gear):
    target_gear = target_gear.upper()
    valid = ["R", "N", "1", "2", "3", "4", "5", "6"]
    if target_gear not in valid:
      print("Invalid gear. Choose R, N, or 1-6.")
      return

    if target_gear == "R" and self.speed > 0:
      print("Stop completely before reversing!")
    elif self.gear == "R" and target_gear not in ["R", "N"] and self.speed > 0:
      print("Stop completely before shifting forward!")
    else:
      self.gear = int(target_gear) if target_gear.isdigit() else target_gear
      print(f"Shifted to gear: {self.gear}")

  def accelerate(self, amount=10):
    if not self.running:
      print("Start the engine first!")
      return

    if self.gear == "N":
      print("Revving in Neutral — not moving!")
    elif self.gear == "R":
      self.speed = min(30, self.speed + amount)
      print(f"Reversing. Speed: {self.speed} km/h")
    else:
      self.speed = min(200, self.speed + amount)
      print(f"Accelerating. Speed: {self.speed} km/h (Gear {self.gear})")

  def brake(self, amount=10):
    self.speed = max(0, self.speed - amount)
    print(f"Slowing down. Speed: {self.speed} km/h")


car = Car()
menu = "Commands: start | stop | gear <R/N/1-6> | accel | brake | status | quit"
print(menu)

while True:
  cmd = input("\nEnter command: ").strip().lower().split()
  if not cmd:
    continue

  action = cmd[0]
  if action == "start":
    car.start()
  elif action == "stop":
    car.stop()
  elif action == "gear" and len(cmd) > 1:
    car.change_gear(cmd[1])
  elif action == "accel":
    car.accelerate()
  elif action == "brake":
    car.brake()
  elif action == "status":
    state = "ON" if car.running else "OFF"
    print(f"Engine: {state} | Gear: {car.gear} | Speed: {car.speed}/200 km/h")
  elif action == "quit":
    print("Exiting simulator.")
    break
  else:
    print(menu)