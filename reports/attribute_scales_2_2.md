# Аналитические шкалы атрибутов

Классификация выполнена по расширенной типологии из лекции: номинальная ID, контентная, категориальная, дихотомическая, порядковая, интервальная, отношений и абсолютная. Тип Python или тип хранения в файле не определяет шкалу сам по себе.

## NYC TLC Yellow Taxi Trip Records

- **Номинальная ID:** `PULocationID`, `DOLocationID`.
- **Категориальная:** `VendorID`, `RatecodeID`, `payment_type`.
- **Дихотомическая:** `store_and_fwd_flag`.
- **Интервальная:** `tpep_pickup_datetime`, `tpep_dropoff_datetime`.
- **Абсолютная:** `passenger_count`.
- **Отношений:** `trip_distance`, `fare_amount`, `extra`, `mta_tax`, `tip_amount`, `tolls_amount`, `improvement_surcharge`, `total_amount`, `congestion_surcharge`, `Airport_fee`, `cbd_congestion_fee`.

`PULocationID` и `DOLocationID` хранятся как целые числа, но среднее или разность идентификаторов зон не имеют смысла. Их используют для группировки и объединения данных.

## Официальный справочник NYC Taxi Zones

- **Номинальная ID:** `OBJECTID`, `LocationID`.
- **Категориальная:** `zone`, `borough`.
- **Контентная:** `geometry`.
- **Отношений:** `Shape_Leng`, `Shape_Area`.

`geometry` представляет полигон зоны и является сложным геопространственным объектом, а не обычным числом или категорией.

## NYPD Motor Vehicle Collisions JSON API

- **Номинальная ID:** `collision_id`, `zip_code`.
- **Категориальная:** `borough`, `contributing_factor_vehicle_1`, `vehicle_type_code1`, `contributing_factor_vehicle_2`, `vehicle_type_code2`, `contributing_factor_vehicle_3`, `contributing_factor_vehicle_4`, `vehicle_type_code_3`, `vehicle_type_code_4`, `contributing_factor_vehicle_5`, `vehicle_type_code_5`.
- **Контентная:** `location`, `on_street_name`, `off_street_name`, `cross_street_name`.
- **Интервальная:** `crash_date`, `crash_time`, `latitude`, `longitude`.
- **Абсолютная:** `number_of_persons_injured`, `number_of_persons_killed`, `number_of_pedestrians_injured`, `number_of_pedestrians_killed`, `number_of_cyclist_injured`, `number_of_cyclist_killed`, `number_of_motorist_injured`, `number_of_motorist_killed`.

Широта и долгота имеют равный шаг, но их нулевые точки условны, поэтому это интервальная, а не шкала отношений. Поле `location` передаётся API как вложенный JSON-объект.

## NYC 311 Service Requests XML

- **Номинальная ID:** `row`, `unique_key`, `incident_zip`, `community_board`, `council_district`, `police_precinct`, `bbl`.
- **Категориальная:** `agency`, `agency_name`, `complaint_type`, `descriptor`, `location_type`, `address_type`, `city`, `status`, `borough`, `open_data_channel_type`, `park_facility_name`, `park_borough`, `descriptor_2`, `vehicle_type`, `facility_type`, `bridge_highway_direction`.
- **Контентная:** `incident_address`, `street_name`, `cross_street_1`, `cross_street_2`, `intersection_street_1`, `intersection_street_2`, `landmark`, `resolution_description`, `location`, `taxi_pick_up_location`, `bridge_highway_name`, `bridge_highway_segment`.
- **Интервальная:** `created_date`, `closed_date`, `resolution_action_updated_date`, `x_coordinate_state_plane`, `y_coordinate_state_plane`, `latitude`, `longitude`.

`row` является технической строкой XML и в аналитических моделях не используется. `unique_key` также применяют только как технический идентификатор. Для 311 дополнительно рассчитываются числовые признаки: час создания, длительность обработки и число обращений в зоне за час. Последний показатель относится к абсолютной шкале.

## Пресс-релизы NYC DOT за январь 2025 года

В этом источнике поля формируются кодом после получения HTML-страниц архива.

- **Категориальная:** `source`, `document_type`, `borough_mentioned`, `topic`.
- **Контентная:** `title`, `url`.
- **Порядковая:** `publication_month`.
- **Интервальная:** `published_date`.
- **Абсолютная:** `title_length`, `text_length`, `transport_word_count`.

`title_length` и `text_length` измеряют число символов, а `transport_word_count` — число найденных транспортных ключевых слов. Это счётные величины с фиксированной единицей, поэтому они относятся к абсолютной шкале.
