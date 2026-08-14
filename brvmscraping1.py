import base64
import json
import random
import time as time_module # Importe le module 'time' et lui donne l'alias 'time_module'
from datetime import datetime, time # Importe la classe 'time' du module 'datetime'

#import pytz

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
#from selenium.webdriver.chrome.service import ServiceN

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException

# Fonctions développées pour l'analyse des infos :
from Analyse_function1 import *
import os
import tkinter as tk
from tkinter import messagebox

import numpy as np
import pandas as pd





# --- Dictionnaires pour stocker les données du marché en mémoire ---
donnees_marche_cache_actuel = {} # Contiendra la dernière valeur connue pour chaque mnémonique

# Nouveaux dictionnaires pour l'historique spécialisé par instrument
# La structure sera:
# {
#   'MNEMONIQUE_1': { 'HORODATAGE_EXEC_1': donnees_specifiques, 'HORODATAGE_EXEC_2': donnees_specifiques, ... },
#   'MNEMONIQUE_2': { ... }
# }
historique_profondeur_marche = {} # Clé: mnemonique -> Clé: horodatage_exec -> Valeur: carnet_ordres
historique_variation_marche = {}  # Clé: mnemonique -> Clé: horodatage_exec -> Valeur: {dernier_cours, variation_pct, volume, ...}
historique_transactions = {}  # Clé: mnemonique -> Clé: horodatage_exec -> Valeur: {dernier_cours, qte_dernier_echange, ...}

BASE_URL = 'https://myaccount.sgibenin.com/index.html'
options = Options()
# options.add_argument('-headless')
# options.add_argument('-no-sandbox')
# options.add_argument('-disable-dev-shm-usage')
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option('excludeSwitches', ['enable-automation'])
options.add_experimental_option('excludeSwitches', ['enable-automation'])
options.add_experimental_option('useAutomationExtension', False)
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
# options.add_argument(f"user-agent={user_agent}")

options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

driver=webdriver.Chrome(options=options)

#print(driver)

def start(driver):
    print("start")
    driver.get(BASE_URL)
    driver.maximize_window()

# Connexion
def signin():
    time_module.sleep(1)
    print(est_deconnecte(driver))

    logininput= driver.find_element(By.XPATH, "/html/body/div[1]/div[2]/div[2]/input[1]")
    logininput.send_keys("MEN_08194")

    passwordinput= driver.find_element(By.XPATH, "/html/body/div[1]/div[2]/div[2]/input[2]")
    passwordinput.send_keys("98765Lucas?")

    Connectbuton= driver.find_element(By.XPATH, "/html/body/div[1]/div[2]/div[2]/input[3]")
    Connectbuton.click()
    print("Connecté")


def est_deconnecte(driver):
    try:
        #driver.find_element(By.XPATH, "/html/body/div[1]/div[2]/div[2]/input[3]")
        time_module.sleep(3)
        driver.find_element(By.XPATH, "/html/body/div[1]/div/div[3]/table/tbody/tr/td[2]/div")
        return True
    except NoSuchElementException:
        return False
    
def get_response_body(driver, request_id):
    """Récupère le corps de la réponse pour une requête donnée."""
    try:
        response = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        return response["body"]
    except Exception as e:
        print(f"Erreur lors de la récupération du corps de la réponse : {e}")
        return None

def surveiller_et_alerter(variable, limite, name_actif):
    """
    Surveille une variable et affiche une fenêtre pop-up d'alerte si elle dépasse une limite.

    Args:
        variable: La variable à surveiller (doit être accessible dans cette fonction).
        limite: La valeur limite à partir de laquelle l'alerte doit être déclenchée.
    """
    if variable < limite:
        print(f"Alerte! La variable ({name_actif}) a dépassé la limite ({limite}).")
        # Créer une fenêtre Tkinter (elle sera masquée)
        root = tk.Tk()
        root.withdraw()  # Masquer la fenêtre principale

        # Afficher la boîte de message d'alerte
        message = "Le rendement de "+name_actif+" est en dessous de "+ str(limite)
        messagebox.showerror("Alerte!", message )

        print("message affiché ")
        # Fermer la fenêtre Tkinter
        #root.destroy()

def signale():

    #Click sur le boutton portefeuille
    link_portefeuille = driver.find_element(By.XPATH, "/html/body/div[1]/div/div[3]/table/tbody/tr/td[2]/div/button")
    link_portefeuille.click()
    time_module.sleep(1)

    link_scroll = "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[4]/div[2]"

    n = 18 # Nombre d'actifs dans le portefeuille

    # Obtenir les rendements des actifs de mon portefeuille sur la SGI
    actifs_rendements = []
    for m in range(2,n) :
        link = "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(m)+"]/td[11]"
        rendement = driver.find_element(By.XPATH, link)
        rendement = rendement.text
        actifs_rendements.append(rendement)
        if m == 12 :
            #target_item_element = driver.find_element(By.XPATH, "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(n)+"]/td[11]")
            driver.execute_script("arguments[0].scrollTop += 50;", link_scroll)
            time_module.sleep(1)
        time_module.sleep(0.1)

    print(actifs_rendements)

    # Obtenir la quantité disponible des actifs de mon portefeuille sur la SGI
    actifs_nbr = []
    time_module.sleep(0.1)
    for m in range(2,n) :
        link = "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(m)+"]/td[7]"
        nbr = driver.find_element(By.XPATH, link)
        nbr = nbr.text
        actifs_nbr.append(nbr)
        if m == 12 :
            #target_item_element = driver.find_element(By.XPATH, "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(n)+"]/td[11]")
            driver.execute_script("arguments[0].scrollTop += 50;", link_scroll)
            time_module.sleep(1)
        time_module.sleep(0.1)
        
    # Obtenir les noms des actifs de mon portefeuille sur la SGI
    actifs_name = []
    time_module.sleep(0.1)
    for m in range(2,n) :
        link = "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(m)+"]/td[2]"
        name = driver.find_element(By.XPATH, link)
        name = name.text
        actifs_name.append(name)
        if m == 12 :
            #target_item_element = driver.find_element(By.XPATH, "/html/body/div[7]/div[2]/div/div[2]/div[2]/div[2]/div/div/table/tbody/tr["+str(n)+"]/td[11]")
            driver.execute_script("arguments[0].scrollTop += 50;", link_scroll)
            time_module.sleep(1)
        time_module.sleep(0.1)
    print(actifs_name)


    #valeur_element = actif1.find_element(By.XPATH, ".//tr[1]/td[1]")
    limite_rouge = -10
    limite_orange = -7
    limite_jaune = 10
    limite_verte = 13

    for i,rendement in enumerate(actifs_rendements):
        if rendement != '':
            rendement = float(rendement)
            if  (rendement <= limite_orange) and (int(actifs_nbr[i]) >2):
                print("#### Attention ! l'actif ",actifs_name[i]," est en zone orange")
                #surveiller_et_alerter(actif1_rendement,limite_orange,actif1_name.text)
            elif  rendement <= limite_rouge and (int(actifs_nbr[i]) >1) :
                print("#### Double Attention ! l'actif ",actifs_name[i]," est en zone rouge, vous devrez céder")
                #surveiller_et_alerter(actif1_rendement,limite_rouge, actif1_name.text)
            elif  (rendement >= limite_jaune) and (int(actifs_nbr[i]) >2) :
                print("#### Félicitations ! l'actif ",actifs_name[i]," est en zone jaune")
            elif  (rendement >= limite_verte) and (int(actifs_nbr[i]) >3):
                print("#### Double Félicitations ! l'actif ",actifs_name[i]," est en zone verte, vous devrez prendre des gains")

count=0
historical_data = []

def collecte( ) :
    # Obtenir l'heure actuelle (seulement la composante horaire)
    heure_actuelle = datetime.now().time()

    # Définir les heures de référence en utilisant la CLASSE time de datetime
    heure_onze = time(11, 0, 0)  # Correct
    heure_onze_un = time(11, 1, 5)  # Correct
    heure_quatorze = time(14, 0, 0) # Correct
    heure_quatorze_un = time(14, 1, 5) # Correct
    heure_seize = time(16, 0, 0) # Correct
    heure_seize_un = time(16, 1, 5) # Correct

    #print(f"Heure actuelle : {heure_actuelle.strftime('%H:%M:%S')}")
    #print(f"Heure de référence 1 : {heure_onze.strftime('%H:%M:%S')}")
    #print(f"Heure de référence 2 : {heure_quatorze.strftime('%H:%M:%S')}")

    #print("\n--- Résultat de la comparaison ---")

    global historical_data
    print("Etat de connexion :" +str(est_deconnecte(driver)))
    
    connect = est_deconnecte(driver)

    while connect==True:

        connect = est_deconnecte(driver)
        if connect==False :
            signin()
            print("Connexion réussit")
            time_module.sleep(3)
            connect = est_deconnecte(driver)

        elif connect==True :
            try :
                if heure_actuelle > heure_onze and heure_actuelle < heure_onze_un :
                    print("######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻###############♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻")
                    signale()
                elif heure_actuelle > heure_quatorze and heure_actuelle < heure_quatorze_un:
                    print("######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻###############♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻")
                    signale()
                elif heure_actuelle > heure_seize and heure_actuelle < heure_seize_un:
                    print("######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻######♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻###############♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻♠♠♣♠♣♠♣♠♣♦♣♦♣♠○◘•♣☻♥☺♥♠♦•◘○♠♥☻☺☻♣☻")
                    signale()
                
                #signale()
                #print("je rentre")
                data = driver.get_log("performance")
                #print("no data")
                #if data:
                #print("je collecte")
                #print(time.strftime("%H:%M:%S"))
                #contenu.click()
                #print(data)
                i=0
                for log in data:
                    log_entry = json.loads(log["message"])["message"]
                    if (
                        "Network.responseReceived" == log_entry["method"]
                        or "Network.requestWillBeSent" == log_entry["method"]
                    ):
                        url = log_entry["params"]["response"]["url"] if "response" in log_entry["params"] else log_entry["params"]["request"]["url"]
                        if "MarketDetails.aspx" in url:  # Filtrer par l'URL qui vous intéresse
                            request_id = log_entry["params"]["requestId"]
                            if "response" in log_entry["params"]:
                                response_body = get_response_body(driver, request_id)
                                if response_body:
                                    print(f"URL: {url}")
                                    print(f"Corps de la réponse : {response_body}")
                                    
                                    historical_data.append([time_module.strftime("%H:%M:%S"), response_body])
                                    if i==0:
                                        traiter_bloc_xml(response_body, donnees_marche_cache_actuel, historique_profondeur_marche, historique_variation_marche, historique_transactions)
                                        Test= donnees_marche_cache_actuel
                                    else :
                                        if Test != donnees_marche_cache_actuel :
                                            traiter_bloc_xml(response_body, donnees_marche_cache_actuel, historique_profondeur_marche, historique_variation_marche, historique_transactions)
                                            Test= donnees_marche_cache_actuel
                                        else :
                                            Test= donnees_marche_cache_actuel
                                    
                                    # Collecter les données à l'état actuel du marché dans la base de données
                                    Collecte_data_BRVM(donnees_marche_cache_actuel)
                                    time_module.sleep(1)

                                    """
                                    # Affichage
                                    for mnemo in ['SIVC', 'BOAB','SIVC']: # Mnémoniques à afficher
                                        if mnemo in donnees_marche_cache_actuel:
                                            print(f"\n--- {mnemo} (Actuel) ---")
                                            pprint(donnees_marche_cache_actuel[mnemo]['dernier_cours'])

                                            if mnemo in historique_profondeur_marche:
                                                print(f"\n--- {mnemo} (Historique Profondeur) ---")
                                                for ts, data in historique_profondeur_marche[mnemo].items():
                                                    print(f"  {ts}: {len(data['carnet_ordres'])} niveaux dans le carnet")
                                                    #print(f" niveaux dans le carnet \n  {ts}: {(data['carnet_ordres'])} ")
                                                    # pprint(data['carnet_ordres'][0] if data['carnet_ordres'] else "Carnet vide") # Afficher le premier niveau pour concision

                                            if mnemo in historique_variation_marche:
                                                print(f"\n--- {mnemo} (Historique Variation) ---")
                                                for ts, data in historique_variation_marche[mnemo].items():
                                                    print(f"  {ts}: Cours={data['dernier_cours']}, Var%={data['variation_pourcentage']:.2f}")

                                            if mnemo in historique_transactions:
                                                print(f"\n--- {mnemo} (Historique Transactions) ---")
                                                for ts, data in historique_transactions[mnemo].items():
                                                    print(f"  {ts}: Cours={data['cours_transaction']}, Qte={data['quantite_transaction']}")
                                    print("------------------------------------")
                                    time.sleep(0.1)
                                    """

                                    # Traiter le corps de la réponse (JSON, HTML, etc.)
                                else:
                                    #print(f"URL: {url}")
                                    print("Aucun corps de réponse disponible.")
                                    print(time_module.time())
                            else:
                                #print(f"URL: {url}")
                                print("Requête envoyée.")
#                               print(time_module.time())

                #maSelection.click()
            except Exception as e :
                print(e)
                print("Tentative de connexion")
                try:
                    signin()
                    time_module.sleep(2)
                    return historical_data
                
                except Exception as e1:
                    print(e1)
                    break
        else :
            signin()
            time_module.sleep(2)
        
    return historical_data


start(driver)
time_module.sleep(5)
signin()
time_module.sleep(2)

time_module.sleep(3)
print(est_deconnecte(driver))
collecte()

