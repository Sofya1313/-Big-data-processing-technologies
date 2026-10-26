"""Конвейер очистки, объединения и обновления данных для пункта 2.3.

Запуск из корня проекта:
    python src/pipeline_2_3.py --month 2025-01

Скрипт не передаёт большие файлы в Git: результаты сохраняются в data/processed.
"""

from __future__ import annotations

import argparse
import re
import time
import warnings
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import geopandas as gpd
import pandas as pd
import requests
from bs4 import BeautifulSoup
from sklearn.feature_extraction.text import TfidfVectorizer


COLLISIONS_URL = "https://data.cityofnewyork.us/resource/h9gi-nx95.json"
SERVICE_311_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.xml"
DOT_ARCHIVE_URL = "https://www.nyc.gov/html/dot/html/pr2025/prindex-2025.shtml"
BOROUGHS = ["Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"]


def project_paths(project_dir: Path) -> tuple[Path, Path]:
    """Возвращает папки исходных и обработанных данных."""
    raw_dir = project_dir / "data" / "raw"
    processed_dir = project_dir / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir, processed_dir


def overview(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    """Таблица размера, типов и пропусков — часть предварительного анализа."""
    # В JSON некоторые поля (например, location) могут быть словарями, а словарь
    # нельзя хешировать. Представление repr позволяет посчитать уникальность и
    # не изменяет исходный набор данных.
    def safe_nunique(series: pd.Series) -> int:
        try:
            return int(series.nunique(dropna=True))
        except TypeError:
            return int(series.map(repr).nunique(dropna=True))

    return pd.DataFrame(
        {
            "source": source,
            "column": frame.columns,
            "dtype": frame.dtypes.astype(str).values,
            "missing_count": frame.isna().sum().values,
            "missing_share": (frame.isna().mean() * 100).round(2).values,
            "unique_count": [safe_nunique(frame[col]) for col in frame.columns],
            "example_value": [
                frame[col].dropna().iloc[0] if frame[col].notna().any() else None
                for col in frame.columns
            ],
        }
    )


def read_taxi(raw_dir: Path, month: str) -> pd.DataFrame:
    path = raw_dir / f"yellow_tripdata_{month}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Не найден основной файл: {path}")
    return pd.read_parquet(path)


def clean_taxi(taxi: pd.DataFrame, month: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Очищает поездки и документирует, сколько строк исключено каждым правилом."""
    result = taxi.copy()
    result["tpep_pickup_datetime"] = pd.to_datetime(result["tpep_pickup_datetime"], errors="coerce")
    result["tpep_dropoff_datetime"] = pd.to_datetime(result["tpep_dropoff_datetime"], errors="coerce")
    result["trip_duration_min"] = (
        result["tpep_dropoff_datetime"] - result["tpep_pickup_datetime"]
    ).dt.total_seconds() / 60

    rules = {
        "есть время начала и окончания": result["tpep_pickup_datetime"].notna() & result["tpep_dropoff_datetime"].notna(),
        "корректные идентификаторы зон": result["PULocationID"].between(1, 263) & result["DOLocationID"].between(1, 263),
        "неотрицательная дистанция": result["trip_distance"].ge(0),
        "длительность от 1 секунды до 3 часов": result["trip_duration_min"].gt(0) & result["trip_duration_min"].le(180),
        "неотрицательная итоговая стоимость": result["total_amount"].ge(0),
    }
    if month is not None:
        month_period = pd.Period(month, freq="M")
        month_start = month_period.start_time
        month_end = (month_period + 1).start_time
        rules["время начала попадает в анализируемый месяц"] = (
            result["tpep_pickup_datetime"].ge(month_start)
            & result["tpep_pickup_datetime"].lt(month_end)
        )
    mask = pd.Series(True, index=result.index)
    audit = []
    for rule_name, rule_mask in rules.items():
        failed = int((mask & ~rule_mask).sum())
        audit.append({"rule": rule_name, "excluded_rows": failed})
        mask &= rule_mask

    result = result.loc[mask].copy()
    result["pickup_date"] = result["tpep_pickup_datetime"].dt.date.astype("string")
    result["pickup_hour"] = result["tpep_pickup_datetime"].dt.hour.astype("int8")
    return result, pd.DataFrame(audit)


def read_zones(raw_dir: Path) -> gpd.GeoDataFrame:
    files = list((raw_dir / "taxi_zones").rglob("*.shp"))
    if not files:
        raise FileNotFoundError("В data/raw/taxi_zones не найден Shapefile (.shp).")
    zones = gpd.read_file(files[0])
    return zones.to_crs("EPSG:4326")


def fetch_collisions(month: str, limit: int = 50_000) -> pd.DataFrame:
    start = f"{month}-01T00:00:00"
    end = (pd.Period(month, freq="M") + 1).start_time.strftime("%Y-%m-%dT00:00:00")
    response = requests.get(
        COLLISIONS_URL,
        params={"$limit": limit, "$where": f"crash_date >= '{start}' AND crash_date < '{end}'"},
        timeout=90,
    )
    response.raise_for_status()
    return pd.DataFrame(response.json())


def point_to_zone(
    frame: pd.DataFrame, zones: gpd.GeoDataFrame, latitude: str = "latitude", longitude: str = "longitude"
) -> gpd.GeoDataFrame:
    """Автоматически назначает точке Taxi Zone; ручное сопоставление строк не нужно."""
    result = frame.copy()
    result[latitude] = pd.to_numeric(result[latitude], errors="coerce")
    result[longitude] = pd.to_numeric(result[longitude], errors="coerce")
    result = result[result[latitude].between(40.4, 40.95) & result[longitude].between(-74.3, -73.65)].copy()
    points = gpd.GeoDataFrame(
        result,
        geometry=gpd.points_from_xy(result[longitude], result[latitude]),
        crs="EPSG:4326",
    )
    zone_columns = zones[["LocationID", "borough", "geometry"]].rename(columns={"borough": "zone_borough"})
    return gpd.sjoin(points, zone_columns, how="left", predicate="within").drop(columns="index_right")


def clean_collisions(collisions: pd.DataFrame, zones: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = collisions.copy()
    # SODA возвращает crash_date как ISO-строку с временем 00:00:00.000.
    # Берём календарную часть и явно задаём формат, чтобы не зависеть от
    # эвристики dateutil и не получать предупреждение о неоднозначной дате.
    crash_date = result["crash_date"].astype("string").str.slice(0, 10)
    crash_time = result["crash_time"].astype("string")
    result["crash_datetime"] = pd.to_datetime(
        crash_date + " " + crash_time, format="%Y-%m-%d %H:%M", errors="coerce"
    )
    for column in ["number_of_persons_injured", "number_of_persons_killed"]:
        result[column] = pd.to_numeric(result.get(column, 0), errors="coerce").fillna(0)
    result = point_to_zone(result, zones)
    result = result.dropna(subset=["crash_datetime", "LocationID"]).copy()
    result["event_date"] = result["crash_datetime"].dt.date.astype("string")
    result["event_hour"] = result["crash_datetime"].dt.hour.astype("int8")
    return result


def fetch_311_xml(raw_dir: Path, month: str, limit: int = 1_000) -> pd.DataFrame:
    """Получает XML-выборку 311 строго за заданный месяц с локальным резервом.

    NYC Open Data иногда обрывает HTTPS-соединение. Поэтому используется один
    запрос с повторными попытками, а при временной сетевой ошибке — ранее
    сохранённый XML-файл того же месяца. Это не меняет период исследования.
    """
    month_period = pd.Period(month, freq="M")
    path = raw_dir / f"nyc_311_{month}_sample.xml"
    start = month_period.start_time.strftime("%Y-%m-%dT00:00:00")
    end = (month_period + 1).start_time.strftime("%Y-%m-%dT00:00:00")
    content = None
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                SERVICE_311_URL,
                params={
                    "$limit": limit,
                    "$order": "created_date ASC",
                    "$where": f"created_date >= '{start}' AND created_date < '{end}'",
                },
                timeout=90,
            )
            response.raise_for_status()
            content = response.content
            path.write_bytes(content)
            break
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)

    if content is None:
        if not path.exists():
            raise RuntimeError(
                "Не удалось получить XML 311 и нет локальной январской копии. "
                "Запустите ячейку 311 в ноутбуке 01 позже и повторите запуск."
            ) from last_error
        warnings.warn(
            f"NYC 311 временно не ответил; используется сохранённая локальная копия: {path.name}",
            RuntimeWarning,
        )
        content = path.read_bytes()

    root = ET.fromstring(content)
    result = pd.DataFrame([{child.tag: child.text for child in row} for row in root.findall(".//row")])
    # Даже при чтении локальной копии дополнительно фиксируем временное окно.
    created = pd.to_datetime(result.get("created_date"), errors="coerce")
    return result.loc[created.dt.to_period("M").eq(month_period)].copy()


def clean_311(service_311: pd.DataFrame, zones: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = service_311.copy()
    result["created_datetime"] = pd.to_datetime(result["created_date"], errors="coerce")
    result["closed_datetime"] = pd.to_datetime(result.get("closed_date"), errors="coerce")
    result["resolution_hours"] = (result["closed_datetime"] - result["created_datetime"]).dt.total_seconds() / 3600
    text = result.get("complaint_type", "").fillna("") + " " + result.get("descriptor", "").fillna("")
    relevant = r"street|traffic|road|signal|sidewalk|parking|highway|bridge|construction|transport"
    result = result[text.str.contains(relevant, case=False, regex=True, na=False)].copy()
    result = point_to_zone(result, zones)
    result = result.dropna(subset=["created_datetime", "LocationID"]).copy()
    result["event_date"] = result["created_datetime"].dt.date.astype("string")
    result["event_hour"] = result["created_datetime"].dt.hour.astype("int8")
    return result


def fetch_dot_text(month: str, limit: int = 10) -> pd.DataFrame:
    """Получает только пресс-релизы целевого месяца и создаёт их разметку."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    archive = session.get(DOT_ARCHIVE_URL, timeout=90)
    archive.raise_for_status()
    links: list[dict[str, str]] = []
    for link in BeautifulSoup(archive.text, "html.parser").select("a[href]"):
        url = urljoin(DOT_ARCHIVE_URL, link["href"])
        title = link.get_text(" ", strip=True)
        if "/pr2025/" in url and title and url not in [item["url"] for item in links]:
            links.append({"title": title, "url": url})

    topic_patterns = {
        "construction": r"construction|resurfacing|repair|work zone",
        "street_safety": r"safety|vision zero|pedestrian|intersection",
        "cycling": r"bike|bicycle|cycling",
        "traffic_management": r"traffic|parking|congestion|street closure",
    }
    transport_words = re.compile(r"\b(traffic|street|road|bridge|transit|bike|safety|parking|construction)\b", re.I)
    rows = []
    target_period = pd.Period(month, freq="M")
    for item in links:
        response = session.get(item["url"], timeout=90)
        response.raise_for_status()
        text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
        date_match = re.search(r"[A-Z][a-z]+\s+\d{1,2},\s+2025", text)
        published_date = pd.to_datetime(date_match.group(0), errors="coerce") if date_match else pd.NaT
        # В контекст января не включаются публикации других месяцев.
        if pd.isna(published_date) or published_date.to_period("M") != target_period:
            continue
        rows.append({
            **item,
            "article_text": text,
            "published_date": published_date,
            "borough_mentioned": next((b for b in BOROUGHS if re.search(rf"\b{b}\b", text, re.I)), "not_specified"),
            "topic": next((topic for topic, pattern in topic_patterns.items() if re.search(pattern, text, re.I)), "other"),
            "text_length": len(text),
            "transport_word_count": len(transport_words.findall(text)),
        })
        if len(rows) >= limit:
            break
    if not rows:
        raise ValueError(f"В архиве NYC DOT не найдены пресс-релизы за {month}.")
    return pd.DataFrame(rows)


def vectorize_text(dot_text: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Векторизует тексты TF-IDF; строки — документы, столбцы — значимые слова/фразы."""
    vectorizer = TfidfVectorizer(stop_words="english", max_features=50, ngram_range=(1, 2), min_df=1)
    matrix = vectorizer.fit_transform(dot_text["article_text"].fillna(""))
    vectors = pd.DataFrame(matrix.toarray(), columns=vectorizer.get_feature_names_out())
    vectors.insert(0, "document_id", dot_text.index)
    vocabulary = pd.DataFrame({"term": vectorizer.get_feature_names_out(), "idf": vectorizer.idf_})
    return vectors, vocabulary.sort_values("idf")


def build_merged_fact(
    taxi: pd.DataFrame, zones: gpd.GeoDataFrame, collisions: gpd.GeoDataFrame,
    service_311: gpd.GeoDataFrame, dot_text: pd.DataFrame,
) -> pd.DataFrame:
    """Создаёт факт-таблицу на уровне Taxi Zone × дата × час."""
    taxi_fact = (
        taxi.groupby(["PULocationID", "pickup_date", "pickup_hour"], as_index=False)
        .agg(
            trip_count=("VendorID", "size"),
            passenger_count_sum=("passenger_count", "sum"),
            trip_distance_mean=("trip_distance", "mean"),
            trip_duration_min_mean=("trip_duration_min", "mean"),
            fare_amount_mean=("fare_amount", "mean"),
            total_amount_mean=("total_amount", "mean"),
            total_amount_sum=("total_amount", "sum"),
        )
        .rename(columns={"PULocationID": "taxi_zone_id", "pickup_date": "event_date", "pickup_hour": "event_hour"})
    )
    zone_dim = zones[["LocationID", "zone", "borough"]].rename(columns={"LocationID": "taxi_zone_id"})
    fact = taxi_fact.merge(zone_dim, on="taxi_zone_id", how="left", validate="many_to_one")

    collision_fact = (
        collisions.groupby(["LocationID", "event_date", "event_hour"], as_index=False)
        .agg(
            collision_count=("collision_id", "size"),
            persons_injured_sum=("number_of_persons_injured", "sum"),
            persons_killed_sum=("number_of_persons_killed", "sum"),
        )
        .rename(columns={"LocationID": "taxi_zone_id"})
    )
    request_fact = (
        service_311.groupby(["LocationID", "event_date", "event_hour"], as_index=False)
        .agg(
            service311_sample_count=("unique_key", "size"),
            resolution_hours_median=("resolution_hours", "median"),
        )
        .rename(columns={"LocationID": "taxi_zone_id"})
    )
    text_fact = (
        dot_text.assign(event_date=dot_text["published_date"].dt.date.astype("string"))
        .groupby(["borough_mentioned", "event_date"], as_index=False)
        .agg(dot_release_count=("title", "size"), dot_transport_word_count=("transport_word_count", "sum"))
        .rename(columns={"borough_mentioned": "borough"})
    )
    keys = ["taxi_zone_id", "event_date", "event_hour"]
    fact = fact.merge(collision_fact, on=keys, how="left", validate="one_to_one")
    fact = fact.merge(request_fact, on=keys, how="left", validate="one_to_one")
    fact = fact.merge(text_fact, on=["borough", "event_date"], how="left", validate="many_to_one")
    for column in ["collision_count", "persons_injured_sum", "persons_killed_sum", "service311_sample_count", "dot_release_count", "dot_transport_word_count"]:
        fact[column] = fact[column].fillna(0).astype("int32")
    return fact


def run_pipeline(project_dir: Path, month: str = "2025-01") -> dict[str, object]:
    """Полное обновление: чтение, очистка, аннотирование, слияние и сохранение."""
    raw_dir, processed_dir = project_paths(project_dir)
    taxi_raw = read_taxi(raw_dir, month)
    taxi, taxi_audit = clean_taxi(taxi_raw, month)
    zones = read_zones(raw_dir)
    collisions_raw = fetch_collisions(month)
    collisions = clean_collisions(collisions_raw, zones)
    service_raw = fetch_311_xml(raw_dir, month)
    service = clean_311(service_raw, zones)
    dot_text = fetch_dot_text(month)
    text_vectors, vocabulary = vectorize_text(dot_text)
    fact = build_merged_fact(taxi, zones, collisions, service, dot_text)

    taxi_audit.to_csv(processed_dir / "taxi_cleaning_audit_2025_01.csv", index=False, encoding="utf-8-sig")
    overview(taxi_raw, "yellow_taxi_raw").to_csv(processed_dir / "taxi_overview_2025_01.csv", index=False, encoding="utf-8-sig")
    overview(collisions_raw, "collisions_json_raw").to_csv(processed_dir / "collisions_overview_2025_01.csv", index=False, encoding="utf-8-sig")
    overview(service_raw, "service_311_xml_raw").to_csv(processed_dir / "service_311_overview_2025_01.csv", index=False, encoding="utf-8-sig")
    dot_text.to_parquet(processed_dir / f"dot_text_annotated_{month}.parquet", index=False)
    text_vectors.to_parquet(processed_dir / f"dot_text_tfidf_vectors_{month}.parquet", index=False)
    vocabulary.to_csv(processed_dir / f"dot_text_tfidf_vocabulary_{month}.csv", index=False, encoding="utf-8-sig")
    fact.to_parquet(processed_dir / f"fact_taxi_zone_hour_{month}.parquet", index=False)

    return {
        "taxi_raw": taxi_raw, "taxi": taxi, "taxi_audit": taxi_audit, "zones": zones,
        "collisions_raw": collisions_raw, "collisions": collisions,
        "service_raw": service_raw, "service": service, "dot_text": dot_text,
        "text_vectors": text_vectors, "vocabulary": vocabulary, "fact": fact,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Обновление объединённой витрины Taxi Zone × дата × час")
    parser.add_argument("--month", default="2025-01", help="месяц основного Parquet-файла, например 2025-01")
    args = parser.parse_args()
    result = run_pipeline(Path(__file__).resolve().parents[1], args.month)
    print(f"Готово. Витрина: {result['fact'].shape[0]:,} строк, {result['fact'].shape[1]} столбцов.")
