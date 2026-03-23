import appdaemon.plugins.hass.hassapi as hass
import socket
import datetime
import collections


class narodmon_sender(hass.Hass):

    def initialize(self):
        self.log("Narodmon PRO sender starting...")

        self.sensors = []
        self.sensors_name = {}
        self.sensors_type = {}
        self.device_data = None

        self.last_values = {}
        self.data_changed = False

        self.last_send_time = None

        # интервалы (в секундах)
        self.send_interval_changed = int(self.args.get("send_interval_changed", 360))  # 6 мин
        self.send_interval_force = int(self.args.get("send_interval_force", 1200))    # 20 мин

        replace = {
            'temperature': 'TEMP',
            'humidity': 'RH',
            'pressure': 'PRESS',
            'battery': 'BATCHARGE',
            'power': 'W',
            'illuminance': 'LIGHT',
            'signal_strength': 'RSSI',
            None: 'SENSOR'
        }

        # --- MAC ---
        mac = self.args.get("narodmon_device_mac")
        if not mac:
            self.error("No MAC address specified!")
            return

        self.device_data = f"#{mac}"

        name = self.args.get("narodmon_device_name")
        if name:
            self.device_data += f"#{name}"

        # --- COORD ---
        coord_entity = self.args.get("hass_coordinates_entity")
        if coord_entity and self.entity_exists(coord_entity):
            lat = self.get_state(coord_entity, attribute="latitude")
            lng = self.get_state(coord_entity, attribute="longitude")

            if lat and lng:
                self.device_data += f"\n#LAT#{lat}\n#LNG#{lng}"

        # --- SENSORS ---
        sensor_list = self.args.get("hass_sensor_entities")
        if not sensor_list:
            self.error("No sensors specified!")
            return

        for entity in sensor_list.split(","):
            entity = entity.strip()

            if not self.entity_exists(entity):
                self.warning(f"Sensor not found: {entity}")
                continue

            domain, sensor_id = self.split_entity(entity)

            if domain != "sensor":
                continue

            self.sensors.append(sensor_id)

            self.sensors_name[sensor_id] = self.get_state(entity, attribute="friendly_name")
            self.sensors_type[sensor_id] = self.get_state(entity, attribute="device_class")

        # --- TYPE NORMALIZATION ---
        for sid in self.sensors_type:
            self.sensors_type[sid] = replace.get(self.sensors_type[sid], "SENSOR")

        count = collections.Counter(self.sensors_type.values())
        for t in count:
            if count[t] > 1:
                i = 1
                for sid in self.sensors_type:
                    if self.sensors_type[sid] == t:
                        self.sensors_type[sid] = f"{t}{i}"
                        i += 1

        # старт через 30 сек
        self.run_in(self.start, 30)


    def start(self, kwargs):
        self.log("Narodmon sender started")

        # подписка на изменения
        for sensor_id in self.sensors:
            self.listen_state(self.on_change, f"sensor.{sensor_id}")

        # основной цикл проверки (раз в минуту)
        self.run_every(self.scheduler, self.datetime(), 60)


    def is_valid(self, state):
        return state not in [None, "unknown", "unavailable"]


    def on_change(self, entity, attribute, old, new, kwargs):
        if not self.is_valid(new):
            return

        sensor_id = entity.split(".")[1]

        if self.last_values.get(sensor_id) != new:
            self.last_values[sensor_id] = new
            self.data_changed = True
            self.log(f"Data changed: {entity} = {new}", level="DEBUG")


    def scheduler(self, kwargs):
        now = self.datetime()

        # если ещё не было отправок
        if self.last_send_time is None:
            self.log("First send")
            self.send_all()
            return

        delta = (now - self.last_send_time).total_seconds()

        # --- если данные менялись ---
        if self.data_changed and delta >= self.send_interval_changed:
            self.log("Sending due to changes")
            self.send_all()
            return

        # --- если давно не отправляли ---
        if delta >= self.send_interval_force:
            self.log("Force send (timeout)")
            self.send_all(force=True)
            return


    def send_all(self, force=False):
        sensors_data = "\n"
        valid_count = 0

        for sensor_id in self.sensors:
            entity = f"sensor.{sensor_id}"
            state = self.get_state(entity)

            if not self.is_valid(state):
                continue

            sensors_data += f"#{self.sensors_type[sensor_id]}#{state}#{self.sensors_name[sensor_id]}\n"
            valid_count += 1

        if valid_count == 0:
            self.log("No valid data to send", level="WARNING")
            return

        data = self.device_data + sensors_data + "##"

        self.log("Sending data:\n" + data)

        try:
            sock = socket.socket()
            sock.settimeout(10)

            sock.connect(("narodmon.ru", 8283))
            sock.send(data.encode("utf-8"))

            reply = sock.recv(1024).decode("utf-8", errors="ignore")
            sock.close()

            self.last_send_time = self.datetime()
            self.data_changed = False

            self.log(f"Server reply: {reply.strip()}")

        except socket.error as err:
            self.error(f"Connection error: {err}")
