"""
scraping_brvm_aiven_24h.py
==========================

Collecte BRVM -> Aiven for MySQL (defaultdb) sur Render.

Fonctionnement :
1. Ouvre Chrome en mode headless.
2. Se connecte au site SGI.
3. Intercepte les réponses Network contenant "MarketDetails.aspx".
4. Utilise traiter_bloc_xml() depuis Analyse_function1.py.
5. Enregistre DIRECTEMENT les données dans Aiven for MySQL.
6. Chaque champ simple du dictionnaire possède sa propre colonne SQL.
7. carnet_ordres reste en JSON.
8. Le premier ID est configuré à 99000000000.
9. La connexion Aiven est persistante et se reconnecte automatiquement.
10. La collecte est autorisée uniquement du lundi au vendredi, de 10h00 à 16h30.
11. Les horaires sont évalués dans le fuseau COLLECT_TIMEZONE (par défaut Africa/Porto-Novo).
12. Hors créneau, Selenium n'est pas lancé et aucune donnée de marché n'est écrite.
13. Un mini serveur HTTP /health est lancé pour que le script puisse être déployé comme Web Service Render.
14. Le script redémarre sa session Selenium après une erreur.
15. La taille de defaultdb est contrôlée périodiquement.
16. Lorsque le seuil configuré est atteint, le programme s'arrête avec le code 99.

Dépendances :
    pip install selenium pymysql python-dotenv cryptography

Fichiers attendus dans le même dossier :
    scraping_brvm_aiven_24h.py
    Analyse_function1.py
    .env
    ca.pem

IMPORTANT :
    - Ce script N'UTILISE PAS Collecte_data_BRVM().
    - Il utilise seulement traiter_bloc_xml().
"""

import copy
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
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from Analyse_function1 import traiter_bloc_xml


# ============================================================
# 1. DOSSIERS ET .ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


# ============================================================
# 2. CONFIGURATION SGI
# ============================================================

SGI_BASE_URL = os.getenv(
    "SGI_BASE_URL",
    "https://myaccount.sgibenin.com/index.html",
).strip()

SGI_LOGIN = os.getenv("SGI_LOGIN", "").strip()
SGI_PASSWORD = os.getenv("SGI_PASSWORD", "")


# ============================================================
# 3. CONFIGURATION AIVEN
# ============================================================

AIVEN_MYSQL_HOST = os.getenv(
    "AIVEN_MYSQL_HOST",
    "",
).strip()

AIVEN_MYSQL_PORT = os.getenv(
    "AIVEN_MYSQL_PORT",
    "",
).strip()

AIVEN_MYSQL_USER = os.getenv(
    "AIVEN_MYSQL_USER",
    "",
).strip()

AIVEN_MYSQL_PASSWORD = os.getenv(
    "AIVEN_MYSQL_PASSWORD",
    "",
)

AIVEN_MYSQL_DATABASE = os.getenv(
    "AIVEN_MYSQL_DATABASE",
    "defaultdb",
).strip() or "defaultdb"

AIVEN_CA_CERT = os.getenv(
    "AIVEN_CA_CERT",
    "ca.pem",
).strip()

AIVEN_MYSQL_TABLE = os.getenv(
    "AIVEN_MYSQL_TABLE",
    "brvm_market_data",
).strip()

AIVEN_FIRST_ID = int(
    os.getenv(
        "AIVEN_FIRST_ID",
        "99000000000",
    )
)


# ============================================================
# 4. HORAIRES DE COLLECTE
# ============================================================

# Render utilise souvent UTC côté serveur. On ne dépend donc jamais
# de l'heure locale de la machine Render.
COLLECT_TIMEZONE_NAME = os.getenv(
    "COLLECT_TIMEZONE",
    "Africa/Porto-Novo",
).strip()

COLLECT_TIMEZONE = ZoneInfo(
    COLLECT_TIMEZONE_NAME
)

COLLECT_START_HOUR = int(
    os.getenv("COLLECT_START_HOUR", "10")
)

COLLECT_START_MINUTE = int(
    os.getenv("COLLECT_START_MINUTE", "0")
)

COLLECT_END_HOUR = int(
    os.getenv("COLLECT_END_HOUR", "16")
)

COLLECT_END_MINUTE = int(
    os.getenv("COLLECT_END_MINUTE", "30")
)

COLLECT_START_TIME = time(
    COLLECT_START_HOUR,
    COLLECT_START_MINUTE,
)

COLLECT_END_TIME = time(
    COLLECT_END_HOUR,
    COLLECT_END_MINUTE,
)

# Délai de vérification hors marché.
OUTSIDE_WINDOW_CHECK_SECONDS = int(
    os.getenv(
        "OUTSIDE_WINDOW_CHECK_SECONDS",
        "60",
    )
)


# ============================================================
# 5. PARAMÈTRES DE FONCTIONNEMENT
# ============================================================

CHROME_HEADLESS = (
    os.getenv(
        "CHROME_HEADLESS",
        "true",
    ).strip().lower()
    in {"1", "true", "yes", "oui"}
)

RESTART_DELAY_SECONDS = int(
    os.getenv(
        "RESTART_DELAY_SECONDS",
        "30",
    )
)

LOOP_SLEEP_SECONDS = float(
    os.getenv(
        "LOOP_SLEEP_SECONDS",
        "0.25",
    )
)

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

# Seuil de sécurité configurable.
# Ce contrôle mesure la taille logique tables + index de defaultdb.
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


# Nombre maximal d'entrées historiques conservées en mémoire
# par instrument. Cela évite une croissance RAM illimitée en 24/7.
MAX_HISTORY_PER_INSTRUMENT = int(
    os.getenv(
        "MAX_HISTORY_PER_INSTRUMENT",
        "200",
    )
)

# Nombre maximal de réponses brutes conservées en RAM.
MAX_RAW_RESPONSES_IN_MEMORY = int(
    os.getenv(
        "MAX_RAW_RESPONSES_IN_MEMORY",
        "500",
    )
)



# ============================================================
# 6. LOGS
# ============================================================

LOG_FILE = BASE_DIR / "collecte_brvm.log"

logger = logging.getLogger("BRVM_AIVEN_24H")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


# ============================================================
# 7. EXCEPTION SPÉCIALE POUR LA LIMITE DE STOCKAGE
# ============================================================

class StorageLimitReached(RuntimeError):
    pass



# ============================================================
# 8. VALIDATION DE LA CONFIGURATION
# ============================================================

def verifier_configuration():
    manquants = []

    if not SGI_LOGIN:
        manquants.append("SGI_LOGIN")

    if not SGI_PASSWORD:
        manquants.append("SGI_PASSWORD")

    if not AIVEN_MYSQL_HOST:
        manquants.append("AIVEN_MYSQL_HOST")

    if not AIVEN_MYSQL_PORT:
        manquants.append("AIVEN_MYSQL_PORT")

    if not AIVEN_MYSQL_USER:
        manquants.append("AIVEN_MYSQL_USER")

    if not AIVEN_MYSQL_PASSWORD:
        manquants.append("AIVEN_MYSQL_PASSWORD")

    if manquants:
        raise RuntimeError(
            "Paramètres manquants dans .env : "
            + ", ".join(manquants)
        )

    if "://" in AIVEN_MYSQL_HOST:
        raise RuntimeError(
            "AIVEN_MYSQL_HOST doit contenir uniquement le hostname, "
            "pas l'URI mysql:// complète."
        )

    try:
        port = int(AIVEN_MYSQL_PORT)
    except ValueError as exc:
        raise RuntimeError(
            "AIVEN_MYSQL_PORT doit être un entier."
        ) from exc

    if not (1 <= port <= 65535):
        raise RuntimeError(
            "AIVEN_MYSQL_PORT est hors plage."
        )

    if not re.fullmatch(
        r"[A-Za-z0-9_]+",
        AIVEN_MYSQL_TABLE,
    ):
        raise RuntimeError(
            "AIVEN_MYSQL_TABLE doit contenir uniquement "
            "lettres, chiffres et underscores."
        )

    if AIVEN_FIRST_ID < 1:
        raise RuntimeError(
            "AIVEN_FIRST_ID doit être supérieur à 0."
        )

    if AIVEN_MAX_STORAGE_MB <= 0:
        raise RuntimeError(
            "AIVEN_MAX_STORAGE_MB doit être supérieur à 0."
        )

    if not (
        0 <= COLLECT_START_HOUR <= 23
        and 0 <= COLLECT_END_HOUR <= 23
        and 0 <= COLLECT_START_MINUTE <= 59
        and 0 <= COLLECT_END_MINUTE <= 59
    ):
        raise RuntimeError(
            "Les horaires de collecte sont invalides."
        )

    if COLLECT_START_TIME >= COLLECT_END_TIME:
        raise RuntimeError(
            "L'heure de début doit être antérieure à l'heure de fin."
        )

    if OUTSIDE_WINDOW_CHECK_SECONDS < 5:
        raise RuntimeError(
            "OUTSIDE_WINDOW_CHECK_SECONDS doit être >= 5."
        )

    if AIVEN_CA_CERT:
        ca_path = Path(AIVEN_CA_CERT).expanduser()

        if not ca_path.is_absolute():
            ca_path = BASE_DIR / ca_path

        if not ca_path.exists():
            raise RuntimeError(
                f"Certificat CA introuvable : {ca_path}"
            )


# ============================================================
# 9. CONVERSIONS DE DONNÉES
# ============================================================

def convertir_decimal(value):
    if value is None:
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None

    text = str(value).strip()

    if not text:
        return None

    if text.lower() in {
        "none",
        "null",
        "nan",
        "n/a",
        "na",
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
        return Decimal(text)

    except InvalidOperation:
        logger.warning(
            "Valeur décimale non convertible : %r",
            value,
        )
        return None


def convertir_entier(value):
    decimal_value = convertir_decimal(value)

    if decimal_value is None:
        return None

    try:
        return int(decimal_value)

    except (
        ValueError,
        TypeError,
        OverflowError,
    ):
        logger.warning(
            "Valeur entière non convertible : %r",
            value,
        )
        return None


def convertir_texte(
    value,
    max_length=None,
):
    if value is None:
        return None

    text = str(value).strip()

    if max_length is not None:
        return text[:max_length]

    return text


def convertir_json(value):
    if value is None:
        value = []

    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
    )


# ============================================================
# 10. CLIENT AIVEN
# ============================================================

class AivenMySQLClient:

    def __init__(self):
        self.connection = None
        self.last_storage_check = 0.0
        self.last_storage_size_mb = 0.0

    def _ssl_configuration(self):
        ca_path = Path(
            AIVEN_CA_CERT
        ).expanduser()

        if not ca_path.is_absolute():
            ca_path = BASE_DIR / ca_path

        return {
            "ca": str(ca_path),
            "check_hostname": True,
        }

    def connect(self):
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
            port=int(AIVEN_MYSQL_PORT),
            user=AIVEN_MYSQL_USER,
            password=AIVEN_MYSQL_PASSWORD,
            database=AIVEN_MYSQL_DATABASE,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=DB_CONNECT_TIMEOUT,
            read_timeout=DB_READ_TIMEOUT,
            write_timeout=DB_WRITE_TIMEOUT,
            cursorclass=pymysql.cursors.DictCursor,
            ssl=self._ssl_configuration(),
        )

        logger.info(
            "Connexion Aiven for MySQL établie."
        )

        return self.connection

    def ensure_connection(self):
        if self.connection is None:
            return self.connect()

        try:
            self.connection.ping(
                reconnect=True
            )

        except Exception:
            logger.exception(
                "Connexion Aiven perdue. Reconnexion."
            )

            return self.connect()

        return self.connection

    def test_connection(self):
        connection = self.ensure_connection()

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
            "Test Aiven réussi | MySQL=%s | Base=%s | User=%s",
            result.get("mysql_version"),
            result.get("database_name"),
            result.get("connected_user"),
        )

        if (
            result.get("database_name")
            != AIVEN_MYSQL_DATABASE
        ):
            raise RuntimeError(
                f"La base active n'est pas {AIVEN_MYSQL_DATABASE!r}."
            )

        return result

    def ensure_table(self):
        connection = self.ensure_connection()

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
                (`collected_at`),

            INDEX `idx_brvm_mnemonique_date`
                (`mnemonique`, `collected_at`)
        )
        ENGINE=InnoDB
        DEFAULT CHARSET=utf8mb4
        COLLATE=utf8mb4_unicode_ci
        """

        try:
            with connection.cursor() as cursor:
                cursor.execute(sql)

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

        self._verifier_schema()

        logger.info(
            "Table prête : %s.%s | AUTO_INCREMENT >= %s",
            AIVEN_MYSQL_DATABASE,
            AIVEN_MYSQL_TABLE,
            AIVEN_FIRST_ID,
        )

    def _verifier_schema(self):
        colonnes_attendues = {
            "id",
            "nom",
            "code_isin",
            "seuil_bas",
            "mnemonique",
            "seuil_haut",
            "valeur_cmp",
            "cours_veille",
            "carnet_ordres",
            "dernier_cours",
            "groupe_marche",
            "cours_max_jour",
            "cours_min_jour",
            "cours_ouverture",
            "valeur_echangee",
            "quantite_echangee",
            "statut_suspension",
            "variation_montant",
            "info_dernier_echange",
            "variation_pourcentage",
            "heure_derniere_execution",
            "prix_theorique_ouverture",
            "quantite_dernier_echange",
            "quantite_theorique_ouverture",
            "horodatage_derniere_execution",
            "variation_theorique_ouverture",
            "collected_at",
        }

        connection = self.ensure_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SHOW COLUMNS
                FROM `{AIVEN_MYSQL_TABLE}`
                """
            )

            result = cursor.fetchall()

        colonnes_actuelles = {
            row["Field"]
            for row in result
        }

        manquantes = (
            colonnes_attendues
            - colonnes_actuelles
        )

        if manquantes:
            raise RuntimeError(
                "Schéma de table incompatible. "
                "Colonnes manquantes : "
                + ", ".join(
                    sorted(manquantes)
                )
            )

    def get_database_size_mb(self):
        connection = self.ensure_connection()

        sql = """
        SELECT
            COALESCE(
                SUM(data_length + index_length),
                0
            ) / 1024 / 1024 AS size_mb
        FROM information_schema.tables
        WHERE table_schema = %s
        """

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                (AIVEN_MYSQL_DATABASE,),
            )

            result = cursor.fetchone()

        return float(
            result.get("size_mb") or 0.0
        )

    def check_storage_limit(
        self,
        force=False,
    ):
        now = time_module.time()

        if (
            not force
            and (
                now
                - self.last_storage_check
                < STORAGE_CHECK_INTERVAL_SECONDS
            )
        ):
            return True

        size_mb = self.get_database_size_mb()

        self.last_storage_check = now
        self.last_storage_size_mb = size_mb

        percentage = (
            size_mb
            / AIVEN_MAX_STORAGE_MB
            * 100
        )

        logger.info(
            "Stockage logique defaultdb : %.2f Mo / %.2f Mo "
            "(%.1f%% du seuil configuré)",
            size_mb,
            AIVEN_MAX_STORAGE_MB,
            percentage,
        )

        if (
            size_mb
            >= AIVEN_MAX_STORAGE_MB
        ):
            raise StorageLimitReached(
                "Seuil de stockage Aiven atteint : "
                f"{size_mb:.2f} Mo / "
                f"{AIVEN_MAX_STORAGE_MB:.2f} Mo"
            )

        return True

    def insert_market_snapshot(
        self,
        donnees_marche,
    ):
        if not donnees_marche:
            return 0

        self.check_storage_limit()

        connection = self.ensure_connection()

        collected_at = datetime.now()

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
            cle_instrument,
            data,
        ) in donnees_marche.items():

            if not isinstance(
                data,
                dict,
            ):
                continue

            mnemonique = convertir_texte(
                data.get(
                    "mnemonique",
                    cle_instrument,
                ),
                64,
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
                        data.get(
                            "statut_suspension"
                        ),
                        255,
                    ),
                    convertir_decimal(
                        data.get("variation_montant")
                    ),
                    convertir_texte(
                        data.get(
                            "info_dernier_echange"
                        ),
                        255,
                    ),
                    convertir_decimal(
                        data.get(
                            "variation_pourcentage"
                        )
                    ),
                    convertir_texte(
                        data.get(
                            "heure_derniere_execution"
                        ),
                        64,
                    ),
                    convertir_decimal(
                        data.get(
                            "prix_theorique_ouverture"
                        )
                    ),
                    convertir_entier(
                        data.get(
                            "quantite_dernier_echange"
                        )
                    ),
                    convertir_entier(
                        data.get(
                            "quantite_theorique_ouverture"
                        )
                    ),
                    convertir_texte(
                        data.get(
                            "horodatage_derniere_execution"
                        ),
                        128,
                    ),
                    convertir_decimal(
                        data.get(
                            "variation_theorique_ouverture"
                        )
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

            logger.info(
                "%s ligne(s) enregistrée(s) dans %s.%s.",
                len(lignes),
                AIVEN_MYSQL_DATABASE,
                AIVEN_MYSQL_TABLE,
            )

            return len(lignes)

        except pymysql.MySQLError as exc:
            connection.rollback()

            message = str(exc).lower()

            if any(
                mot in message
                for mot in (
                    "disk full",
                    "no space",
                    "out of space",
                    "quota",
                    "table is full",
                )
            ):
                raise StorageLimitReached(
                    f"Aiven/MySQL signale un stockage plein : {exc}"
                ) from exc

            raise

    def close(self):
        if (
            self.connection is not None
        ):
            try:
                self.connection.close()

            except Exception:
                pass

            finally:
                self.connection = None


db = AivenMySQLClient()


# ============================================================
# 11. DONNÉES EN MÉMOIRE
# ============================================================

donnees_marche_cache_actuel = {}

historique_profondeur_marche = {}
historique_variation_marche = {}
historique_transactions = {}

historical_data = deque(maxlen=MAX_RAW_RESPONSES_IN_MEMORY)


# ============================================================
# 12. CALENDRIER DE COLLECTE + SERVEUR HTTP RENDER
# ============================================================

def maintenant_marche():
    """
    Heure courante dans le fuseau choisi pour la collecte.
    """
    return datetime.now(
        COLLECT_TIMEZONE
    )


def est_jour_ouvre(moment=None):
    """
    Lundi=0 ... vendredi=4.
    Samedi et dimanche sont toujours exclus.
    """
    if moment is None:
        moment = maintenant_marche()

    return moment.weekday() < 5


def est_dans_fenetre_collecte(moment=None):
    """
    True uniquement :
      - du lundi au vendredi ;
      - entre 10h00 et 16h30 incluses
        (ou les valeurs définies dans .env).
    """
    if moment is None:
        moment = maintenant_marche()

    if not est_jour_ouvre(moment):
        return False

    heure = moment.time().replace(
        tzinfo=None
    )

    return (
        COLLECT_START_TIME
        <= heure
        <= COLLECT_END_TIME
    )


def prochaine_ouverture(moment=None):
    """
    Calcule la prochaine ouverture du créneau de collecte.
    """
    if moment is None:
        moment = maintenant_marche()

    # Si nous sommes avant l'ouverture d'un jour ouvré,
    # l'ouverture est aujourd'hui.
    if (
        est_jour_ouvre(moment)
        and moment.time().replace(tzinfo=None)
        < COLLECT_START_TIME
    ):
        return datetime.combine(
            moment.date(),
            COLLECT_START_TIME,
            tzinfo=COLLECT_TIMEZONE,
        )

    # Sinon, recherche le prochain jour ouvré.
    for decalage in range(1, 8):
        prochaine_date = (
            moment.date()
            + timedelta(days=decalage)
        )

        if prochaine_date.weekday() < 5:
            return datetime.combine(
                prochaine_date,
                COLLECT_START_TIME,
                tzinfo=COLLECT_TIMEZONE,
            )

    raise RuntimeError(
        "Impossible de calculer la prochaine ouverture."
    )


def attendre_fenetre_collecte():
    """
    Hors marché, ne lance pas Selenium.
    Le processus reste disponible pour le serveur HTTP Render.
    """
    while True:
        moment = maintenant_marche()

        if est_dans_fenetre_collecte(
            moment
        ):
            logger.info(
                "Fenêtre de collecte ouverte | %s",
                moment.isoformat(),
            )
            return

        prochaine = prochaine_ouverture(
            moment
        )

        secondes = max(
            1,
            int(
                (
                    prochaine
                    - moment
                ).total_seconds()
            ),
        )

        logger.info(
            "Collecte inactive | maintenant=%s | "
            "prochaine ouverture=%s",
            moment.strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
            prochaine.strftime(
                "%Y-%m-%d %H:%M:%S %Z"
            ),
        )

        time_module.sleep(
            min(
                OUTSIDE_WINDOW_CHECK_SECONDS,
                secondes,
            )
        )


class RenderHealthHandler(
    BaseHTTPRequestHandler
):
    """
    Petit endpoint HTTP pour Render :
      GET /
      GET /health
    """

    def do_GET(self):
        moment = maintenant_marche()

        payload = {
            "status": "ok",
            "service": "brvm-collector",
            "timezone": COLLECT_TIMEZONE_NAME,
            "local_time": moment.isoformat(),
            "weekday": moment.strftime("%A"),
            "collection_window_open": (
                est_dans_fenetre_collecte(
                    moment
                )
            ),
            "collection_schedule": (
                f"Monday-Friday "
                f"{COLLECT_START_TIME.strftime('%H:%M')}"
                f"-{COLLECT_END_TIME.strftime('%H:%M')}"
            ),
            "database": AIVEN_MYSQL_DATABASE,
            "table": AIVEN_MYSQL_TABLE,
        }

        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(
        self,
        format,
        *args,
    ):
        # Évite de polluer collecte_brvm.log à chaque health check.
        return


def demarrer_serveur_render():
    """
    Un Web Service Render doit écouter le port fourni par $PORT.
    """
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        RenderHealthHandler,
    )

    thread = threading.Thread(
        target=server.serve_forever,
        name="render-health-server",
        daemon=True,
    )

    thread.start()

    logger.info(
        "Serveur HTTP Render actif sur 0.0.0.0:%s "
        "(/ et /health)",
        port,
    )

    return server


# ============================================================
# 13. SELENIUM
# ============================================================

def creer_driver():
    options = Options()

    if CHROME_HEADLESS:
        options.add_argument(
            "--headless=new"
        )

    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )

    options.add_argument(
        "--disable-dev-shm-usage"
    )

    options.add_argument(
        "--no-sandbox"
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

    options.set_capability(
        "goog:loggingPrefs",
        {
            "performance": "ALL",
        },
    )

    driver_instance = webdriver.Chrome(
        options=options
    )

    return driver_instance


def start(
    driver_instance,
):
    logger.info(
        "Ouverture SGI : %s",
        SGI_BASE_URL,
    )

    driver_instance.get(
        SGI_BASE_URL
    )


def est_connecte(
    driver_instance,
):
    try:
        driver_instance.find_element(
            By.XPATH,
            (
                "/html/body/div[1]/div/div[3]"
                "/table/tbody/tr/td[2]/div"
            ),
        )

        return True

    except NoSuchElementException:
        return False


def signin(
    driver_instance,
):
    logger.info(
        "Connexion au compte SGI."
    )

    login_input = driver_instance.find_element(
        By.XPATH,
        (
            "/html/body/div[1]/div[2]"
            "/div[2]/input[1]"
        ),
    )

    password_input = driver_instance.find_element(
        By.XPATH,
        (
            "/html/body/div[1]/div[2]"
            "/div[2]/input[2]"
        ),
    )

    connect_button = driver_instance.find_element(
        By.XPATH,
        (
            "/html/body/div[1]/div[2]"
            "/div[2]/input[3]"
        ),
    )

    login_input.clear()
    login_input.send_keys(
        SGI_LOGIN
    )

    password_input.clear()
    password_input.send_keys(
        SGI_PASSWORD
    )

    connect_button.click()


def assurer_connexion_sgi(
    driver_instance,
):
    if est_connecte(
        driver_instance
    ):
        return

    signin(
        driver_instance
    )

    time_module.sleep(
        3
    )

    if not est_connecte(
        driver_instance
    ):
        raise RuntimeError(
            "Impossible de confirmer la connexion SGI."
        )


def get_response_body(
    driver_instance,
    request_id,
):
    try:
        response = (
            driver_instance
            .execute_cdp_cmd(
                "Network.getResponseBody",
                {
                    "requestId": request_id,
                },
            )
        )

        return response.get(
            "body"
        )

    except Exception as exc:
        logger.debug(
            "Body indisponible requestId=%s : %s",
            request_id,
            exc,
        )

        return None


# ============================================================
# 14. ALERTES PORTEFEUILLE
# ============================================================

def _normaliser_float(value):
    result = convertir_decimal(
        value
    )

    if result is None:
        return None

    return float(result)


def _normaliser_entier(value):
    result = convertir_entier(
        value
    )

    if result is None:
        return 0

    return result


def signale(
    driver_instance,
):
    """
    Contrôle du portefeuille.
    En mode 24h/headless, on écrit les alertes dans les logs.
    Aucun popup Tkinter n'est utilisé.
    """

    logger.info(
        "Contrôle du portefeuille SGI."
    )

    link_portefeuille = (
        driver_instance.find_element(
            By.XPATH,
            (
                "/html/body/div[1]/div/div[3]"
                "/table/tbody/tr/td[2]/div/button"
            ),
        )
    )

    link_portefeuille.click()

    time_module.sleep(
        1
    )

    n = 18

    actifs_rendements = []
    actifs_nbr = []
    actifs_name = []

    for m in range(2, n):
        xpath = (
            "/html/body/div[7]/div[2]/div/div[2]"
            "/div[2]/div[2]/div/div/table/tbody"
            f"/tr[{m}]/td[11]"
        )

        actifs_rendements.append(
            driver_instance.find_element(
                By.XPATH,
                xpath,
            ).text
        )

    for m in range(2, n):
        xpath = (
            "/html/body/div[7]/div[2]/div/div[2]"
            "/div[2]/div[2]/div/div/table/tbody"
            f"/tr[{m}]/td[7]"
        )

        actifs_nbr.append(
            driver_instance.find_element(
                By.XPATH,
                xpath,
            ).text
        )

    for m in range(2, n):
        xpath = (
            "/html/body/div[7]/div[2]/div/div[2]"
            "/div[2]/div[2]/div/div/table/tbody"
            f"/tr[{m}]/td[2]"
        )

        actifs_name.append(
            driver_instance.find_element(
                By.XPATH,
                xpath,
            ).text
        )

    limite_rouge = -10
    limite_orange = -7
    limite_jaune = 10
    limite_verte = 13

    for (
        index,
        rendement_brut,
    ) in enumerate(
        actifs_rendements
    ):
        rendement = _normaliser_float(
            rendement_brut
        )

        quantite = _normaliser_entier(
            actifs_nbr[index]
        )

        if rendement is None:
            continue

        if (
            rendement <= limite_rouge
            and quantite > 1
        ):
            logger.warning(
                "ZONE ROUGE | %s | rendement=%s",
                actifs_name[index],
                rendement,
            )

        elif (
            rendement <= limite_orange
            and quantite > 2
        ):
            logger.warning(
                "ZONE ORANGE | %s | rendement=%s",
                actifs_name[index],
                rendement,
            )

        elif (
            rendement >= limite_verte
            and quantite > 3
        ):
            logger.info(
                "ZONE VERTE | %s | rendement=%s",
                actifs_name[index],
                rendement,
            )

        elif (
            rendement >= limite_jaune
            and quantite > 2
        ):
            logger.info(
                "ZONE JAUNE | %s | rendement=%s",
                actifs_name[index],
                rendement,
            )


# ============================================================
# 15. NETTOYAGE DES HISTORIQUES EN MÉMOIRE
# ============================================================

def limiter_historique_par_instrument(historique):
    """
    Les fonctions de parsing peuvent accumuler des horodatages sans limite.
    On conserve seulement les N entrées les plus récentes par instrument.
    """

    for mnemonique, sous_historique in list(historique.items()):
        if not isinstance(sous_historique, dict):
            continue

        depassement = (
            len(sous_historique)
            - MAX_HISTORY_PER_INSTRUMENT
        )

        if depassement <= 0:
            continue

        # Les dictionnaires Python conservent l'ordre d'insertion.
        anciennes_cles = list(
            sous_historique.keys()
        )[:depassement]

        for cle in anciennes_cles:
            sous_historique.pop(
                cle,
                None,
            )


def nettoyer_historiques_memoire():
    limiter_historique_par_instrument(
        historique_profondeur_marche
    )

    limiter_historique_par_instrument(
        historique_variation_marche
    )

    limiter_historique_par_instrument(
        historique_transactions
    )


# ============================================================
# 16. TRAITEMENT DES RÉPONSES MARCHÉ
# ============================================================

def traiter_reponse_marche(
    response_body,
    dernier_snapshot,
):
    global historical_data

    historical_data.append(
        [
            time_module.strftime(
                "%H:%M:%S"
            ),
            response_body,
        ]
    )

    traiter_bloc_xml(
        response_body,
        donnees_marche_cache_actuel,
        historique_profondeur_marche,
        historique_variation_marche,
        historique_transactions,
    )

    nettoyer_historiques_memoire()

    snapshot_actuel = copy.deepcopy(
        donnees_marche_cache_actuel
    )

    if not snapshot_actuel:
        return dernier_snapshot

    if (
        dernier_snapshot is not None
        and snapshot_actuel == dernier_snapshot
    ):
        return dernier_snapshot

    db.insert_market_snapshot(
        snapshot_actuel
    )

    return snapshot_actuel


# ============================================================
# 17. BOUCLE DE COLLECTE
# ============================================================

def collecte(
    driver_instance,
):
    heure_onze = time(
        11,
        0,
        0,
    )

    heure_onze_fin = time(
        11,
        1,
        5,
    )

    heure_quatorze = time(
        14,
        0,
        0,
    )

    heure_quatorze_fin = time(
        14,
        1,
        5,
    )

    heure_seize = time(
        16,
        0,
        0,
    )

    heure_seize_fin = time(
        16,
        1,
        5,
    )

    dernier_snapshot = None
    alertes_executees = set()
    dernier_check_session = 0.0

    logger.info(
        "Boucle de collecte démarrée."
    )

    while True:
        # ----------------------------------------------------
        # FIN IMMÉDIATE DE LA SESSION HORS CRÉNEAU
        # ----------------------------------------------------

        moment_marche = maintenant_marche()

        if not est_dans_fenetre_collecte(
            moment_marche
        ):
            logger.info(
                "Fermeture de la session Selenium : "
                "fin du créneau de collecte (%s).",
                moment_marche.strftime(
                    "%Y-%m-%d %H:%M:%S %Z"
                ),
            )
            return

        # ----------------------------------------------------
        # Vérifie la session SGI toutes les 60 secondes
        # ----------------------------------------------------

        now_epoch = time_module.time()

        if (
            now_epoch
            - dernier_check_session
            >= 60
        ):
            assurer_connexion_sgi(
                driver_instance
            )

            dernier_check_session = (
                now_epoch
            )

        # ----------------------------------------------------
        # Contrôle périodique du stockage même si le marché
        # n'envoie pas de nouvelles données
        # ----------------------------------------------------

        db.check_storage_limit()

        # ----------------------------------------------------
        # Contrôles portefeuille aux heures prévues
        # ----------------------------------------------------

        maintenant = maintenant_marche()

        heure_actuelle = maintenant.time().replace(tzinfo=None)
        date_actuelle = maintenant.date()

        fenetres_alertes = [
            (
                "11H",
                heure_onze,
                heure_onze_fin,
            ),
            (
                "14H",
                heure_quatorze,
                heure_quatorze_fin,
            ),
            (
                "16H",
                heure_seize,
                heure_seize_fin,
            ),
        ]

        for (
            nom_fenetre,
            debut,
            fin,
        ) in fenetres_alertes:

            cle = (
                date_actuelle,
                nom_fenetre,
            )

            if (
                debut
                <= heure_actuelle
                <= fin
                and cle
                not in alertes_executees
            ):
                try:
                    signale(
                        driver_instance
                    )

                except Exception as exc:
                    logger.exception(
                        "Erreur pendant le contrôle portefeuille %s : %s",
                        nom_fenetre,
                        exc,
                    )

                alertes_executees.add(
                    cle
                )

        # ----------------------------------------------------
        # Lecture des événements réseau Chrome
        # ----------------------------------------------------

        logs = driver_instance.get_log(
            "performance"
        )

        for log in logs:
            try:
                message = json.loads(
                    log["message"]
                ).get(
                    "message",
                    {},
                )

            except (
                json.JSONDecodeError,
                TypeError,
                KeyError,
            ):
                continue

            if (
                message.get("method")
                != "Network.responseReceived"
            ):
                continue

            params = message.get(
                "params",
                {},
            )

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

            if not request_id:
                continue

            response_body = (
                get_response_body(
                    driver_instance,
                    request_id,
                )
            )

            if not response_body:
                continue

            dernier_snapshot = (
                traiter_reponse_marche(
                    response_body,
                    dernier_snapshot,
                )
            )

        time_module.sleep(
            LOOP_SLEEP_SECONDS
        )


# ============================================================
# 18. UNE SESSION COMPLETE CHROME + SGI
# ============================================================

def executer_session_collecte():
    driver_instance = None

    if not est_dans_fenetre_collecte():
        return

    try:
        driver_instance = creer_driver()

        start(
            driver_instance
        )

        time_module.sleep(
            5
        )

        assurer_connexion_sgi(
            driver_instance
        )

        logger.info(
            "Connexion SGI confirmée."
        )

        collecte(
            driver_instance
        )

    finally:
        if (
            driver_instance is not None
        ):
            try:
                driver_instance.quit()

            except Exception:
                pass


# ============================================================
# 19. SUPERVISEUR RENDER
# ============================================================

def main():
    verifier_configuration()

    # Render Web Service : ouvre le port HTTP dès le démarrage.
    demarrer_serveur_render()

    logger.info(
        "=================================================="
    )
    logger.info(
        "DÉMARRAGE COLLECTEUR BRVM SUR RENDER"
    )
    logger.info(
        "Destination : %s.%s",
        AIVEN_MYSQL_DATABASE,
        AIVEN_MYSQL_TABLE,
    )
    logger.info(
        "Horaire : lundi-vendredi %s-%s | fuseau=%s",
        COLLECT_START_TIME.strftime("%H:%M"),
        COLLECT_END_TIME.strftime("%H:%M"),
        COLLECT_TIMEZONE_NAME,
    )
    logger.info(
        "Premier ID : %s",
        AIVEN_FIRST_ID,
    )
    logger.info(
        "Seuil stockage configuré : %.2f Mo",
        AIVEN_MAX_STORAGE_MB,
    )
    logger.info(
        "Chrome headless : %s",
        CHROME_HEADLESS,
    )
    logger.info(
        "=================================================="
    )

    while True:
        try:
            # Hors horaires : ni Chrome ni collecte SQL.
            attendre_fenetre_collecte()

            # Nous ne préparons Aiven qu'au moment où le marché ouvre.
            db.test_connection()
            db.ensure_table()
            db.check_storage_limit(
                force=True
            )

            logger.info(
                "Ouverture du créneau : lancement Selenium."
            )

            executer_session_collecte()

            # La fonction retourne normalement après 16h30.
            db.close()

        except StorageLimitReached as exc:
            logger.critical(
                "=================================================="
            )
            logger.critical(
                "ARRÊT VOLONTAIRE : LIMITE DE STOCKAGE"
            )
            logger.critical(
                "%s",
                exc,
            )
            logger.critical(
                "=================================================="
            )

            db.close()
            sys.exit(99)

        except KeyboardInterrupt:
            logger.info(
                "Arrêt manuel demandé."
            )

            db.close()
            sys.exit(0)

        except Exception as exc:
            logger.exception(
                "Erreur de session : %s",
                exc,
            )

            db.close()

            # Si l'erreur arrive après la fermeture du marché,
            # on ne boucle pas inutilement sur Selenium.
            if not est_dans_fenetre_collecte():
                continue

            logger.info(
                "Redémarrage automatique dans %s secondes...",
                RESTART_DELAY_SECONDS,
            )

            time_module.sleep(
                RESTART_DELAY_SECONDS
            )


# ============================================================
# 20. POINT D'ENTRÉE
# ============================================================

if __name__ == "__main__":
    main()