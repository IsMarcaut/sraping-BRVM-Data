"""
scraping_brvm_render_aiven.py
=============================

Adaptation Render + Aiven du scraper local brvmscraping1.py.

PRINCIPE IMPORTANT
------------------
La logique de collecte reste celle qui fonctionne en local :

    driver.get_log("performance")
            ↓
    Network.responseReceived
            ↓
    URL contient MarketDetails.aspx
            ↓
    Network.getResponseBody(requestId)
            ↓
    traiter_bloc_xml(...)
            ↓
    insertion Aiven

AUCUN polling artificiel de MarketDetails.aspx.
AUCUN replay fetch().
Le site SGI reste maître du rythme des mises à jour.

Différences nécessaires pour Render :
- Chrome/Chromium headless ;
- credentials dans les variables d'environnement ;
- mini serveur HTTP /health ;
- collecte lundi-vendredi, 10h00-16h30 Bénin ;
- connexion Aiven MySQL / defaultdb ;
- reconnexion automatique ;
- retries courts sur getResponseBody ;
- fallback sur Network.loadingFinished si le body n'est pas encore
  disponible au moment de responseReceived ;
- pas de dépendance pyodbc/SQL Server/Windows.
"""

import base64
import json
import logging
import math
import os
import re
import sys
import threading
import time as time_module

from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
from dotenv import load_dotenv

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from Analyse_function1_render import traiter_bloc_xml


# ============================================================
# 1. DOSSIER / ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


# ============================================================
# 2. SGI
# ============================================================

SGI_BASE_URL = os.getenv(
    "SGI_BASE_URL",
    "https://myaccount.sgibenin.com/index.html",
).strip()

SGI_LOGIN = os.getenv(
    "SGI_LOGIN",
    "",
).strip()

SGI_PASSWORD = os.getenv(
    "SGI_PASSWORD",
    "",
)


# ============================================================
# 3. AIVEN
# ============================================================

AIVEN_MYSQL_HOST = os.getenv(
    "AIVEN_MYSQL_HOST",
    "",
).strip().strip('"').strip("'")

AIVEN_MYSQL_PORT = os.getenv(
    "AIVEN_MYSQL_PORT",
    "",
).strip().strip('"').strip("'")

AIVEN_MYSQL_USER = os.getenv(
    "AIVEN_MYSQL_USER",
    "",
).strip().strip('"').strip("'")

AIVEN_MYSQL_PASSWORD = os.getenv(
    "AIVEN_MYSQL_PASSWORD",
    "",
)

AIVEN_MYSQL_DATABASE = (
    os.getenv(
        "AIVEN_MYSQL_DATABASE",
        "defaultdb",
    )
    .strip()
    .strip('"')
    .strip("'")
    or "defaultdb"
)

AIVEN_CA_CERT = (
    os.getenv(
        "AIVEN_CA_CERT",
        "/etc/secrets/ca.pem",
    )
    .strip()
    .strip('"')
    .strip("'")
)

AIVEN_MYSQL_TABLE = (
    os.getenv(
        "AIVEN_MYSQL_TABLE",
        "brvm_market_data",
    )
    .strip()
    .strip('"')
    .strip("'")
)

AIVEN_FIRST_ID = int(
    os.getenv(
        "AIVEN_FIRST_ID",
        "99000000000",
    )
)


# ============================================================
# 4. HORAIRES DE COLLECTE
# ============================================================

COLLECT_TIMEZONE_NAME = os.getenv(
    "COLLECT_TIMEZONE",
    "Africa/Porto-Novo",
).strip()

COLLECT_TIMEZONE = ZoneInfo(
    COLLECT_TIMEZONE_NAME
)

COLLECT_START_TIME = time(
    int(os.getenv("COLLECT_START_HOUR", "10")),
    int(os.getenv("COLLECT_START_MINUTE", "0")),
)

COLLECT_END_TIME = time(
    int(os.getenv("COLLECT_END_HOUR", "16")),
    int(os.getenv("COLLECT_END_MINUTE", "30")),
)

OUTSIDE_WINDOW_CHECK_SECONDS = int(
    os.getenv(
        "OUTSIDE_WINDOW_CHECK_SECONDS",
        "60",
    )
)


# ============================================================
# 5. SELENIUM / ROBUSTESSE
# ============================================================

CHROME_HEADLESS = (
    os.getenv(
        "CHROME_HEADLESS",
        "true",
    ).strip().lower()
    in {"1", "true", "yes", "oui"}
)

CHROME_BIN = os.getenv(
    "CHROME_BIN",
    "/usr/bin/chromium",
).strip()

CHROMEDRIVER_PATH = os.getenv(
    "CHROMEDRIVER_PATH",
    "/usr/bin/chromedriver",
).strip()

LOOP_SLEEP_SECONDS = float(
    os.getenv(
        "LOOP_SLEEP_SECONDS",
        "0.10",
    )
)

RESTART_DELAY_SECONDS = int(
    os.getenv(
        "RESTART_DELAY_SECONDS",
        "30",
    )
)

SESSION_CHECK_SECONDS = int(
    os.getenv(
        "SESSION_CHECK_SECONDS",
        "60",
    )
)

# Le code local appelle getResponseBody immédiatement.
# Sur Render, le serveur peut être légèrement plus lent.
# Ces retries gardent la logique locale tout en la fiabilisant.
RESPONSE_BODY_RETRIES = int(
    os.getenv(
        "RESPONSE_BODY_RETRIES",
        "6",
    )
)

RESPONSE_BODY_RETRY_DELAY = float(
    os.getenv(
        "RESPONSE_BODY_RETRY_DELAY",
        "0.15",
    )
)


# ============================================================
# 6. MYSQL / STOCKAGE
# ============================================================

DB_CONNECT_TIMEOUT = int(
    os.getenv(
        "DB_CONNECT_TIMEOUT",
        "10",
    )
)

DB_READ_TIMEOUT = int(
    os.getenv(
        "DB_READ_TIMEOUT",
        "30",
    )
)

DB_WRITE_TIMEOUT = int(
    os.getenv(
        "DB_WRITE_TIMEOUT",
        "30",
    )
)

AIVEN_MAX_STORAGE_MB = float(
    os.getenv(
        "AIVEN_MAX_STORAGE_MB",
        "950",
    )
)

STORAGE_CHECK_INTERVAL_SECONDS = int(
    os.getenv(
        "STORAGE_CHECK_INTERVAL_SECONDS",
        "300",
    )
)


# ============================================================
# 7. LIMITES MEMOIRE
# ============================================================

MAX_RAW_RESPONSES_IN_MEMORY = int(
    os.getenv(
        "MAX_RAW_RESPONSES_IN_MEMORY",
        "200",
    )
)

# Les historiques spécialisés du parseur local grandissent à chaque
# mise à jour. Sur Render, on borne uniquement la RAM ; le cache courant
# et les insertions Aiven restent inchangés.
MAX_HISTORY_PER_INSTRUMENT = int(
    os.getenv(
        "MAX_HISTORY_PER_INSTRUMENT",
        "200",
    )
)

MAX_PENDING_REQUESTS = int(
    os.getenv(
        "MAX_PENDING_REQUESTS",
        "500",
    )
)


# ============================================================
# 8. LOGGING
# ============================================================

LOG_FILE = BASE_DIR / "collecte_brvm.log"

logger = logging.getLogger(
    "BRVM_RENDER_AIVEN"
)

logger.setLevel(
    logging.INFO
)

logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        formatter
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )

    file_handler.setFormatter(
        formatter
    )

    logger.addHandler(
        console_handler
    )

    logger.addHandler(
        file_handler
    )


# ============================================================
# 9. ETAT RUNTIME / HEALTH
# ============================================================

runtime_state = {
    "market_events_seen": 0,
    "market_bodies_processed": 0,
    "rows_inserted": 0,
    "last_market_event_at": None,
    "last_insert_at": None,
    "last_error": None,
}


# ============================================================
# 10. EXCEPTIONS
# ============================================================

class StorageLimitReached(
    RuntimeError
):
    pass


# ============================================================
# 11. VALIDATION
# ============================================================

def verifier_configuration():

    manquants = []

    for nom, valeur in [
        ("SGI_LOGIN", SGI_LOGIN),
        ("SGI_PASSWORD", SGI_PASSWORD),
        ("AIVEN_MYSQL_HOST", AIVEN_MYSQL_HOST),
        ("AIVEN_MYSQL_PORT", AIVEN_MYSQL_PORT),
        ("AIVEN_MYSQL_USER", AIVEN_MYSQL_USER),
        ("AIVEN_MYSQL_PASSWORD", AIVEN_MYSQL_PASSWORD),
    ]:
        if not valeur:
            manquants.append(
                nom
            )

    if manquants:
        raise RuntimeError(
            "Variables Render manquantes : "
            + ", ".join(
                manquants
            )
        )

    # Un hostname doit être un hostname, pas une URI Aiven.
    if (
        "://" in AIVEN_MYSQL_HOST
        or "@" in AIVEN_MYSQL_HOST
        or "/" in AIVEN_MYSQL_HOST
        or "?" in AIVEN_MYSQL_HOST
        or ":" in AIVEN_MYSQL_HOST
    ):
        raise RuntimeError(
            "AIVEN_MYSQL_HOST doit contenir uniquement "
            "le hostname Aiven."
        )

    int(
        AIVEN_MYSQL_PORT
    )

    if not re.fullmatch(
        r"[A-Za-z0-9_]+",
        AIVEN_MYSQL_TABLE,
    ):
        raise RuntimeError(
            "Nom de table Aiven invalide."
        )

    ca_path = Path(
        AIVEN_CA_CERT
    )

    if not ca_path.is_absolute():
        ca_path = (
            BASE_DIR
            / ca_path
        )

    if not ca_path.exists():
        raise RuntimeError(
            f"Certificat CA introuvable : {ca_path}"
        )


# ============================================================
# 12. CONVERSIONS MYSQL
# ============================================================

def convertir_decimal(
    value,
):
    if value is None:
        return None

    if isinstance(
        value,
        Decimal,
    ):
        return value

    if isinstance(
        value,
        float,
    ):
        if (
            math.isnan(value)
            or math.isinf(value)
        ):
            return None

    text = str(
        value
    ).strip()

    if not text:
        return None

    if text.lower() in {
        "none",
        "null",
        "nan",
        "n/a",
        "na",
        "-",
    }:
        return None

    text = (
        text
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace("%", "")
        .replace(",", ".")
    )

    try:
        return Decimal(
            text
        )

    except InvalidOperation:
        return None


def convertir_entier(
    value,
):
    decimal_value = (
        convertir_decimal(
            value
        )
    )

    if decimal_value is None:
        return None

    try:
        return int(
            decimal_value
        )

    except Exception:
        return None


def convertir_texte(
    value,
    max_length=None,
):
    if value is None:
        return None

    text = str(
        value
    ).strip()

    if max_length:
        text = text[
            :max_length
        ]

    return text


def convertir_json(
    value,
):
    return json.dumps(
        value or [],
        ensure_ascii=False,
        default=str,
    )


# ============================================================
# 13. AIVEN MYSQL
# ============================================================

class AivenMySQLClient:

    def __init__(
        self,
    ):
        self.connection = None
        self.last_storage_check = 0.0

    def _ssl(
        self,
    ):
        ca_path = Path(
            AIVEN_CA_CERT
        )

        if not ca_path.is_absolute():
            ca_path = (
                BASE_DIR
                / ca_path
            )

        return {
            "ca": str(
                ca_path
            ),
            "check_hostname": True,
        }

    def connect(
        self,
    ):
        self.close()

        logger.info(
            "Connexion Aiven MySQL | host=%s | port=%s | "
            "database=%s | user=%s",
            AIVEN_MYSQL_HOST,
            AIVEN_MYSQL_PORT,
            AIVEN_MYSQL_DATABASE,
            AIVEN_MYSQL_USER,
        )

        self.connection = pymysql.connect(
            host=AIVEN_MYSQL_HOST,
            port=int(
                AIVEN_MYSQL_PORT
            ),
            user=AIVEN_MYSQL_USER,
            password=AIVEN_MYSQL_PASSWORD,
            database=AIVEN_MYSQL_DATABASE,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=DB_CONNECT_TIMEOUT,
            read_timeout=DB_READ_TIMEOUT,
            write_timeout=DB_WRITE_TIMEOUT,
            cursorclass=pymysql.cursors.DictCursor,
            ssl=self._ssl(),
        )

        logger.info(
            "Connexion Aiven établie."
        )

        return self.connection

    def ensure_connection(
        self,
    ):
        if self.connection is None:
            return self.connect()

        try:
            self.connection.ping(
                reconnect=True
            )

        except Exception:
            return self.connect()

        return self.connection

    def test_connection(
        self,
    ):
        connection = (
            self.ensure_connection()
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    VERSION() AS mysql_version,
                    DATABASE() AS database_name,
                    CURRENT_USER() AS connected_user
                """
            )

            result = cursor.fetchone()

        logger.info(
            "Aiven OK | MySQL=%s | Base=%s | User=%s",
            result.get(
                "mysql_version"
            ),
            result.get(
                "database_name"
            ),
            result.get(
                "connected_user"
            ),
        )

    def ensure_table(
        self,
    ):
        connection = (
            self.ensure_connection()
        )

        sql = f"""
        CREATE TABLE IF NOT EXISTS `{AIVEN_MYSQL_TABLE}` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            `nom` VARCHAR(255) NULL,
            `code_isin` VARCHAR(64) NULL,
            `seuil_bas` DECIMAL(30,10) NULL,
            `mnemonique` VARCHAR(64) NOT NULL,
            `seuil_haut` DECIMAL(30,10) NULL,
            `valeur_cmp` DECIMAL(30,10) NULL,
            `cours_veille` DECIMAL(30,10) NULL,
            `carnet_ordres` JSON NULL,
            `dernier_cours` DECIMAL(30,10) NULL,
            `groupe_marche` VARCHAR(64) NULL,
            `cours_max_jour` DECIMAL(30,10) NULL,
            `cours_min_jour` DECIMAL(30,10) NULL,
            `cours_ouverture` DECIMAL(30,10) NULL,
            `valeur_echangee` DECIMAL(36,10) NULL,
            `quantite_echangee` BIGINT NULL,
            `statut_suspension` VARCHAR(255) NULL,
            `variation_montant` DECIMAL(30,10) NULL,
            `info_dernier_echange` VARCHAR(255) NULL,
            `variation_pourcentage` DECIMAL(30,10) NULL,
            `heure_derniere_execution` VARCHAR(64) NULL,
            `prix_theorique_ouverture` DECIMAL(30,10) NULL,
            `quantite_dernier_echange` BIGINT NULL,
            `quantite_theorique_ouverture` BIGINT NULL,
            `horodatage_derniere_execution` VARCHAR(128) NULL,
            `variation_theorique_ouverture` DECIMAL(30,10) NULL,
            `collected_at` DATETIME(6) NOT NULL,

            PRIMARY KEY (`id`),

            INDEX `idx_brvm_mnemonique`
                (`mnemonique`),

            INDEX `idx_brvm_code_isin`
                (`code_isin`),

            INDEX `idx_brvm_collected_at`
                (`collected_at`)
        )
        ENGINE=InnoDB
        DEFAULT CHARSET=utf8mb4
        COLLATE=utf8mb4_unicode_ci
        """

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql
                )

                cursor.execute(
                    f"""
                    ALTER TABLE `{AIVEN_MYSQL_TABLE}`
                    AUTO_INCREMENT = {AIVEN_FIRST_ID}
                    """
                )

            connection.commit()

        except Exception:
            connection.rollback()
            raise

        logger.info(
            "Table prête : %s.%s",
            AIVEN_MYSQL_DATABASE,
            AIVEN_MYSQL_TABLE,
        )

    def get_database_size_mb(
        self,
    ):
        connection = (
            self.ensure_connection()
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(
                        SUM(data_length + index_length),
                        0
                    ) / 1024 / 1024 AS size_mb
                FROM information_schema.tables
                WHERE table_schema = %s
                """,
                (
                    AIVEN_MYSQL_DATABASE,
                ),
            )

            result = cursor.fetchone()

        return float(
            result.get(
                "size_mb"
            )
            or 0.0
        )

    def check_storage_limit(
        self,
        force=False,
    ):
        maintenant = (
            time_module.time()
        )

        if (
            not force
            and maintenant
            - self.last_storage_check
            < STORAGE_CHECK_INTERVAL_SECONDS
        ):
            return

        taille = (
            self.get_database_size_mb()
        )

        self.last_storage_check = (
            maintenant
        )

        logger.info(
            "Stockage defaultdb : %.2f Mo / %.2f Mo",
            taille,
            AIVEN_MAX_STORAGE_MB,
        )

        if (
            taille
            >= AIVEN_MAX_STORAGE_MB
        ):
            raise StorageLimitReached(
                f"Seuil Aiven atteint : "
                f"{taille:.2f} Mo"
            )

    def insert_market_snapshot(
        self,
        donnees_marche,
    ):
        """
        Fidèle à la logique locale :
        chaque réponse MarketDetails valide déclenche l'insertion
        du snapshot courant complet.

        On ne fait PAS de déduplication entre deux réponses,
        parce que le scraper local appelle Collecte_data_BRVM()
        à chaque réponse MarketDetails.
        """

        self.check_storage_limit()

        connection = (
            self.ensure_connection()
        )

        collected_at = (
            datetime.now(
                COLLECT_TIMEZONE
            )
            .replace(
                tzinfo=None
            )
        )

        sql = f"""
        INSERT INTO `{AIVEN_MYSQL_TABLE}`
        (
            `nom`,
            `code_isin`,
            `seuil_bas`,
            `mnemonique`,
            `seuil_haut`,
            `valeur_cmp`,
            `cours_veille`,
            `carnet_ordres`,
            `dernier_cours`,
            `groupe_marche`,
            `cours_max_jour`,
            `cours_min_jour`,
            `cours_ouverture`,
            `valeur_echangee`,
            `quantite_echangee`,
            `statut_suspension`,
            `variation_montant`,
            `info_dernier_echange`,
            `variation_pourcentage`,
            `heure_derniere_execution`,
            `prix_theorique_ouverture`,
            `quantite_dernier_echange`,
            `quantite_theorique_ouverture`,
            `horodatage_derniere_execution`,
            `variation_theorique_ouverture`,
            `collected_at`
        )
        VALUES
        (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s
        )
        """

        lignes = []

        for (
            cle,
            data,
        ) in donnees_marche.items():

            if str(
                cle
            ).startswith(
                "_"
            ):
                continue

            if not isinstance(
                data,
                dict,
            ):
                continue

            mnemonique = (
                convertir_texte(
                    data.get(
                        "mnemonique",
                        cle,
                    ),
                    64,
                )
            )

            if not mnemonique:
                continue

            lignes.append(
                (
                    convertir_texte(
                        data.get("nom"),
                        255,
                    ),
                    convertir_texte(
                        data.get("code_isin"),
                        64,
                    ),
                    convertir_decimal(
                        data.get("seuil_bas")
                    ),
                    mnemonique,
                    convertir_decimal(
                        data.get("seuil_haut")
                    ),
                    convertir_decimal(
                        data.get("valeur_cmp")
                    ),
                    convertir_decimal(
                        data.get("cours_veille")
                    ),
                    convertir_json(
                        data.get(
                            "carnet_ordres",
                            [],
                        )
                    ),
                    convertir_decimal(
                        data.get("dernier_cours")
                    ),
                    convertir_texte(
                        data.get("groupe_marche"),
                        64,
                    ),
                    convertir_decimal(
                        data.get("cours_max_jour")
                    ),
                    convertir_decimal(
                        data.get("cours_min_jour")
                    ),
                    convertir_decimal(
                        data.get("cours_ouverture")
                    ),
                    convertir_decimal(
                        data.get("valeur_echangee")
                    ),
                    convertir_entier(
                        data.get("quantite_echangee")
                    ),
                    convertir_texte(
                        data.get("statut_suspension"),
                        255,
                    ),
                    convertir_decimal(
                        data.get("variation_montant")
                    ),
                    convertir_texte(
                        data.get("info_dernier_echange"),
                        255,
                    ),
                    convertir_decimal(
                        data.get("variation_pourcentage")
                    ),
                    convertir_texte(
                        data.get("heure_derniere_execution"),
                        64,
                    ),
                    convertir_decimal(
                        data.get("prix_theorique_ouverture")
                    ),
                    convertir_entier(
                        data.get("quantite_dernier_echange")
                    ),
                    convertir_entier(
                        data.get("quantite_theorique_ouverture")
                    ),
                    convertir_texte(
                        data.get("horodatage_derniere_execution"),
                        128,
                    ),
                    convertir_decimal(
                        data.get("variation_theorique_ouverture")
                    ),
                    collected_at,
                )
            )

        if not lignes:
            return 0

        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    sql,
                    lignes,
                )

            connection.commit()

        except Exception:
            connection.rollback()
            raise

        runtime_state[
            "rows_inserted"
        ] += len(
            lignes
        )

        runtime_state[
            "last_insert_at"
        ] = datetime.now(
            COLLECT_TIMEZONE
        ).isoformat()

        logger.info(
            "%s ligne(s) enregistrée(s) dans Aiven.",
            len(
                lignes
            ),
        )

        return len(
            lignes
        )

    def close(
        self,
    ):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None


db = AivenMySQLClient()


# ============================================================
# 14. CACHES IDENTIQUES AU LOCAL
# ============================================================

donnees_marche_cache_actuel = {}
historique_profondeur_marche = {}
historique_variation_marche = {}
historique_transactions = {}

historical_data = deque(
    maxlen=MAX_RAW_RESPONSES_IN_MEMORY
)


# ============================================================
# 15. HORAIRES
# ============================================================

def maintenant_marche():
    return datetime.now(
        COLLECT_TIMEZONE
    )


def est_dans_fenetre_collecte(
    moment=None,
):
    if moment is None:
        moment = maintenant_marche()

    # lundi=0 ... vendredi=4
    if moment.weekday() >= 5:
        return False

    heure = (
        moment.time()
        .replace(
            tzinfo=None
        )
    )

    return (
        COLLECT_START_TIME
        <= heure
        <= COLLECT_END_TIME
    )


def prochaine_ouverture(
    moment=None,
):
    if moment is None:
        moment = maintenant_marche()

    heure = (
        moment.time()
        .replace(
            tzinfo=None
        )
    )

    if (
        moment.weekday()
        < 5
        and heure
        < COLLECT_START_TIME
    ):
        return datetime.combine(
            moment.date(),
            COLLECT_START_TIME,
            tzinfo=COLLECT_TIMEZONE,
        )

    for jours in range(
        1,
        8,
    ):
        date_cible = (
            moment.date()
            + timedelta(
                days=jours
            )
        )

        if (
            date_cible.weekday()
            < 5
        ):
            return datetime.combine(
                date_cible,
                COLLECT_START_TIME,
                tzinfo=COLLECT_TIMEZONE,
            )

    raise RuntimeError(
        "Prochaine ouverture introuvable."
    )


def attendre_ouverture():
    while not est_dans_fenetre_collecte():

        maintenant = (
            maintenant_marche()
        )

        prochaine = (
            prochaine_ouverture(
                maintenant
            )
        )

        logger.info(
            "Hors séance | prochaine collecte : %s",
            prochaine.strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
        )

        time_module.sleep(
            OUTSIDE_WINDOW_CHECK_SECONDS
        )


# ============================================================
# 16. HEALTH RENDER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):
    def do_GET(
        self,
    ):
        payload = {
            "status": "ok",
            "service": "brvm-render-aiven",
            "time": maintenant_marche().isoformat(),
            "collection_window_open": est_dans_fenetre_collecte(),
            **runtime_state,
        }

        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode(
            "utf-8"
        )

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "application/json",
        )

        self.send_header(
            "Content-Length",
            str(
                len(body)
            ),
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def log_message(
        self,
        *args,
    ):
        return


def demarrer_health_server():
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            port,
        ),
        HealthHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )

    thread.start()

    logger.info(
        "Health Render actif sur port %s.",
        port,
    )


# ============================================================
# 17. CHROME
# ============================================================

def creer_driver():
    options = Options()

    if CHROME_HEADLESS:
        options.add_argument(
            "--headless=new"
        )

    options.binary_location = (
        CHROME_BIN
    )

    options.add_argument(
        "--no-sandbox"
    )

    options.add_argument(
        "--disable-dev-shm-usage"
    )

    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )

    options.add_argument(
        "--window-size=1920,1080"
    )

    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation"],
    )

    options.add_experimental_option(
        "useAutomationExtension",
        False,
    )

    # EXACTEMENT le mécanisme utilisé en local.
    options.set_capability(
        "goog:loggingPrefs",
        {
            "performance": "ALL",
        },
    )

    service = Service(
        executable_path=CHROMEDRIVER_PATH
    )

    driver = webdriver.Chrome(
        service=service,
        options=options,
    )

    driver.set_page_load_timeout(
        30
    )

    driver.set_script_timeout(
        30
    )

    driver.execute_cdp_cmd(
        "Network.enable",
        {}
    )

    return driver


# ============================================================
# 18. CONNEXION SGI
# ============================================================

XPATH_CONNECTE = (
    "/html/body/div[1]/div/div[3]"
    "/table/tbody/tr/td[2]/div"
)

XPATH_LOGIN = (
    "/html/body/div[1]/div[2]"
    "/div[2]/input[1]"
)

XPATH_PASSWORD = (
    "/html/body/div[1]/div[2]"
    "/div[2]/input[2]"
)

XPATH_CONNECT_BUTTON = (
    "/html/body/div[1]/div[2]"
    "/div[2]/input[3]"
)


def start_sgi(
    driver,
):
    logger.info(
        "Ouverture SGI."
    )

    driver.get(
        SGI_BASE_URL
    )


def est_connecte(
    driver,
    timeout=5,
):
    try:
        WebDriverWait(
            driver,
            timeout,
        ).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    XPATH_CONNECTE,
                )
            )
        )

        return True

    except TimeoutException:
        return False


def signin(
    driver,
):
    logger.info(
        "Authentification SGI."
    )

    wait = WebDriverWait(
        driver,
        30,
    )

    login = wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                XPATH_LOGIN,
            )
        )
    )

    password = wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                XPATH_PASSWORD,
            )
        )
    )

    bouton = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                XPATH_CONNECT_BUTTON,
            )
        )
    )

    login.clear()
    login.send_keys(
        SGI_LOGIN
    )

    password.clear()
    password.send_keys(
        SGI_PASSWORD
    )

    bouton.click()

    if not est_connecte(
        driver,
        timeout=30,
    ):
        raise RuntimeError(
            "Connexion SGI non confirmée."
        )

    logger.info(
        "Connexion SGI confirmée."
    )


def assurer_connexion_sgi(
    driver,
):
    if est_connecte(
        driver,
        timeout=3,
    ):
        return

    signin(
        driver
    )


# ============================================================
# 19. GET RESPONSE BODY — LOGIQUE LOCALE + RETRIES RENDER
# ============================================================

def get_response_body_once(
    driver,
    request_id,
):
    response = (
        driver.execute_cdp_cmd(
            "Network.getResponseBody",
            {
                "requestId": request_id,
            },
        )
    )

    body = response.get(
        "body"
    )

    if (
        body
        and response.get(
            "base64Encoded"
        )
    ):
        body = (
            base64.b64decode(
                body
            )
            .decode(
                "utf-8",
                errors="replace",
            )
        )

    return body


def get_response_body(
    driver,
    request_id,
):
    """
    Le local appelle getResponseBody immédiatement.
    On fait pareil.

    La seule différence est un retry court pour absorber
    la latence Render/Chromium.
    """

    derniere_erreur = None

    for tentative in range(
        RESPONSE_BODY_RETRIES
    ):
        try:
            body = get_response_body_once(
                driver,
                request_id,
            )

            if body:
                return body

        except Exception as exc:
            derniere_erreur = exc

        if (
            tentative
            < RESPONSE_BODY_RETRIES
            - 1
        ):
            time_module.sleep(
                RESPONSE_BODY_RETRY_DELAY
            )

    if derniere_erreur:
        logger.debug(
            "Body MarketDetails pas encore disponible "
            "| requestId=%s | erreur=%s",
            request_id,
            derniere_erreur,
        )

    return None


# ============================================================
# 20. LIMITATION DES HISTORIQUES EN RAM
# ============================================================

def limiter_historique(
    historique,
):
    for (
        mnemonique,
        sous_historique,
    ) in list(
        historique.items()
    ):

        if not isinstance(
            sous_historique,
            dict,
        ):
            continue

        depassement = (
            len(sous_historique)
            - MAX_HISTORY_PER_INSTRUMENT
        )

        if depassement <= 0:
            continue

        anciennes_cles = list(
            sous_historique.keys()
        )[
            :depassement
        ]

        for cle in anciennes_cles:
            sous_historique.pop(
                cle,
                None,
            )


def nettoyer_historiques_ram():
    limiter_historique(
        historique_profondeur_marche
    )

    limiter_historique(
        historique_variation_marche
    )

    limiter_historique(
        historique_transactions
    )


# ============================================================
# 21. TRAITEMENT D'UNE REPONSE
# ============================================================

def traiter_marketdetails_body(
    response_body,
):
    """
    Reproduit la séquence locale :

        historical_data.append(...)
        traiter_bloc_xml(...)
        Collecte_data_BRVM(...)

    Sauf que Collecte_data_BRVM() est remplacée par
    l'insertion directe Aiven.
    """

    if not response_body:
        return

    historical_data.append(
        [
            maintenant_marche().strftime(
                "%H:%M:%S"
            ),
            response_body,
        ]
    )

    resultat = traiter_bloc_xml(
        response_body,
        donnees_marche_cache_actuel,
        historique_profondeur_marche,
        historique_variation_marche,
        historique_transactions,
    )

    if not resultat:
        logger.warning(
            "Bloc MarketDetails reçu mais XML non traité."
        )
        return

    nettoyer_historiques_ram()

    runtime_state[
        "market_bodies_processed"
    ] += 1

    # IDENTIQUE au comportement effectif du local :
    # insertion à chaque body MarketDetails correctement parsé.
    db.insert_market_snapshot(
        donnees_marche_cache_actuel
    )


# ============================================================
# 22. COLLECTE — CALQUEE SUR brvmscraping1.py
# ============================================================

def collecte(
    driver,
):
    """
    Le coeur de cette fonction suit volontairement
    brvmscraping1.py.

    On lit driver.get_log("performance") en boucle et on traite
    chaque Network.responseReceived de MarketDetails.aspx.
    """

    logger.info(
        "Collecte réseau SGI démarrée."
    )

    dernier_check_session = 0.0

    # Fallback :
    # si responseReceived arrive un peu avant que le body soit
    # disponible sur Render, on réessaye lorsque loadingFinished
    # apparaît.
    pending_marketdetails = {}

    # Pour éviter un double traitement :
    processed_ids = set()
    processed_order = deque(
        maxlen=1000
    )

    def marquer_traite(
        request_id,
    ):
        if request_id in processed_ids:
            return

        if (
            len(processed_order)
            == processed_order.maxlen
        ):
            ancien = processed_order[
                0
            ]

            processed_ids.discard(
                ancien
            )

        processed_order.append(
            request_id
        )

        processed_ids.add(
            request_id
        )

    while est_dans_fenetre_collecte():

        maintenant_epoch = (
            time_module.time()
        )

        # -----------------------------------------------
        # Session SGI
        # -----------------------------------------------

        if (
            maintenant_epoch
            - dernier_check_session
            >= SESSION_CHECK_SECONDS
        ):
            assurer_connexion_sgi(
                driver
            )

            dernier_check_session = (
                maintenant_epoch
            )

        # -----------------------------------------------
        # Stockage Aiven
        # -----------------------------------------------

        db.check_storage_limit()

        # -----------------------------------------------
        # EXACTEMENT comme le local :
        # lire les performance logs en continu.
        # -----------------------------------------------

        data = driver.get_log(
            "performance"
        )

        for log in data:

            try:
                log_entry = json.loads(
                    log["message"]
                )[
                    "message"
                ]

            except Exception:
                continue

            method = log_entry.get(
                "method"
            )

            params = log_entry.get(
                "params",
                {},
            )

            # ===========================================
            # NETWORK.RESPONSERECEIVED
            # ===========================================

            if (
                method
                == "Network.responseReceived"
            ):
                response = params.get(
                    "response",
                    {},
                )

                url = response.get(
                    "url",
                    "",
                )

                if (
                    "MarketDetails.aspx"
                    not in url
                ):
                    continue

                request_id = params.get(
                    "requestId"
                )

                if (
                    not request_id
                    or request_id
                    in processed_ids
                ):
                    continue

                runtime_state[
                    "market_events_seen"
                ] += 1

                runtime_state[
                    "last_market_event_at"
                ] = maintenant_marche().isoformat()

                logger.info(
                    "MarketDetails reçu #%s | requestId=%s",
                    runtime_state[
                        "market_events_seen"
                    ],
                    request_id,
                )

                # ---------------------------------------
                # Même tentative immédiate que le local
                # ---------------------------------------

                body = get_response_body(
                    driver,
                    request_id,
                )

                if body:

                    traiter_marketdetails_body(
                        body
                    )

                    marquer_traite(
                        request_id
                    )

                    pending_marketdetails.pop(
                        request_id,
                        None,
                    )

                else:
                    # Sur le PC local, le body est généralement
                    # déjà disponible. Sur Render, on garde la
                    # requête en attente au lieu de la perdre.
                    pending_marketdetails[
                        request_id
                    ] = {
                        "url": url,
                        "created_at": time_module.time(),
                    }

                continue

            # ===========================================
            # NETWORK.LOADINGFINISHED
            # Fallback Render uniquement.
            # ===========================================

            if (
                method
                == "Network.loadingFinished"
            ):
                request_id = params.get(
                    "requestId"
                )

                if (
                    request_id
                    not in pending_marketdetails
                    or request_id
                    in processed_ids
                ):
                    continue

                body = get_response_body(
                    driver,
                    request_id,
                )

                if body:

                    logger.info(
                        "Body MarketDetails récupéré au "
                        "loadingFinished | requestId=%s",
                        request_id,
                    )

                    traiter_marketdetails_body(
                        body
                    )

                    marquer_traite(
                        request_id
                    )

                else:
                    logger.warning(
                        "Body MarketDetails indisponible "
                        "après loadingFinished | requestId=%s",
                        request_id,
                    )

                pending_marketdetails.pop(
                    request_id,
                    None,
                )

                continue

            # ===========================================
            # NETWORK.LOADINGFAILED
            # ===========================================

            if (
                method
                == "Network.loadingFailed"
            ):
                request_id = params.get(
                    "requestId"
                )

                if (
                    request_id
                    in pending_marketdetails
                ):
                    logger.warning(
                        "MarketDetails réseau échoué "
                        "| requestId=%s | erreur=%s",
                        request_id,
                        params.get(
                            "errorText"
                        ),
                    )

                    pending_marketdetails.pop(
                        request_id,
                        None,
                    )

        # -----------------------------------------------
        # Nettoyage de sécurité
        # -----------------------------------------------

        expiration = (
            time_module.time()
            - 60
        )

        ids_expires = [
            request_id
            for (
                request_id,
                info,
            ) in pending_marketdetails.items()
            if info.get(
                "created_at",
                0,
            )
            < expiration
        ]

        for request_id in ids_expires:
            pending_marketdetails.pop(
                request_id,
                None,
            )

        time_module.sleep(
            LOOP_SLEEP_SECONDS
        )

    logger.info(
        "Fin du créneau de collecte."
    )


# ============================================================
# 23. SESSION CHROME
# ============================================================

def executer_session():

    driver = None

    try:
        driver = creer_driver()

        start_sgi(
            driver
        )

        time_module.sleep(
            5
        )

        if not est_connecte(
            driver,
            timeout=3,
        ):
            signin(
                driver
            )

        # Petite attente identique dans l'esprit du local
        # avant d'entrer dans la boucle performance logs.
        time_module.sleep(
            2
        )

        collecte(
            driver
        )

    finally:

        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# ============================================================
# 24. MAIN RENDER
# ============================================================

def main():

    verifier_configuration()

    demarrer_health_server()

    logger.info(
        "=================================================="
    )

    logger.info(
        "BRVM RENDER + AIVEN"
    )

    logger.info(
        "Collecte : lundi-vendredi %s-%s | %s",
        COLLECT_START_TIME.strftime(
            "%H:%M"
        ),
        COLLECT_END_TIME.strftime(
            "%H:%M"
        ),
        COLLECT_TIMEZONE_NAME,
    )

    logger.info(
        "Logique réseau : identique au scraper local."
    )

    logger.info(
        "=================================================="
    )

    while True:

        try:

            attendre_ouverture()

            db.test_connection()

            db.ensure_table()

            db.check_storage_limit(
                force=True
            )

            executer_session()

            db.close()

        except StorageLimitReached as exc:

            runtime_state[
                "last_error"
            ] = str(
                exc
            )

            logger.critical(
                "%s",
                exc,
            )

            db.close()

            sys.exit(
                99
            )

        except KeyboardInterrupt:

            db.close()

            sys.exit(
                0
            )

        except Exception as exc:

            runtime_state[
                "last_error"
            ] = str(
                exc
            )

            logger.exception(
                "Erreur session : %s",
                exc,
            )

            db.close()

            if not est_dans_fenetre_collecte():
                continue

            logger.info(
                "Nouvelle session dans %s secondes.",
                RESTART_DELAY_SECONDS,
            )

            time_module.sleep(
                RESTART_DELAY_SECONDS
            )


# ============================================================
# 25. POINT D'ENTREE
# ============================================================

if __name__ == "__main__":
    main()