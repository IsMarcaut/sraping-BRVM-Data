"""
Analyse_function1_render.py

Version Render/Linux allégée de Analyse_function1.py.

Cette version conserve UNIQUEMENT la logique réellement utilisée
par le scraper BRVM :
- parsing des PAC_DET ;
- mise à jour du cache actuel ;
- historique profondeur ;
- historique variation ;
- historique transactions ;
- traitement du bloc XML MKT.

Aucune dépendance Windows :
- pas de pyodbc ;
- pas de winsound ;
- pas de tkinter ;
- pas d'Excel/OpenPyXL ;
- pas de SQL Server local.
"""

import xml.etree.ElementTree as ET
from datetime import datetime

def _try_parse_float(valeur_str):
    if valeur_str is None or valeur_str == '' or valeur_str == '0': return None
    try:
        valeur_nettoyee = valeur_str.replace(' ', '').replace(',', '.')
        return float(valeur_nettoyee)
    except (ValueError, TypeError): return None

def _try_parse_int(valeur_str):
    if valeur_str is None or valeur_str == '' or valeur_str == '0': return None
    try:
        valeur_nettoyee = valeur_str.replace(' ', '')
        return int(valeur_nettoyee)
    except (ValueError, TypeError): return None

def _parse_prix_achat_vente(valeur_str):
    if valeur_str is None or valeur_str == '': return None
    valeur_str = valeur_str.strip()
    if valeur_str == '0': return None
    if valeur_str.upper() in ['A', 'O', 'MARCHÉ', 'MARCHE']: return valeur_str.upper()
    return _try_parse_float(valeur_str)

def _get_champ(champs, index, defaut=''):
    try:
        return champs[index].strip() if champs[index] is not None else defaut
    except IndexError: return defaut

def parse_et_maj_caches_specialises(pac_det_chaine, cache_actuel, hist_prof, hist_var, hist_trans):
    pac_det_chaine = pac_det_chaine.strip()
    maj_effectuee = False

    try:
        if pac_det_chaine.startswith("~Re/!"):
            partie_donnees = pac_det_chaine.split("!", 1)[1]
            champs = partie_donnees.split('|')

            mnemonique = _get_champ(champs, 0)
            if not mnemonique:
                # print(f"AVERT: Ligne d'instrument sans mnémonique: {pac_det_chaine}") # Optionnel
                return False

            # --- Parsing des champs (identique à avant) ---
            code_isin = _get_champ(champs, 1)
            groupe_marche = _get_champ(champs, 2)
            cours_veille = _try_parse_float(_get_champ(champs, 3))
            cours_min_jour = _try_parse_float(_get_champ(champs, 4))
            cours_max_jour = _try_parse_float(_get_champ(champs, 5))
            dernier_cours = _try_parse_float(_get_champ(champs, 6))
            variation_pct_str = _get_champ(champs, 7)
            variation_pct = _try_parse_float(variation_pct_str) if variation_pct_str != '-' else 0.0
            quantite_echangee = _try_parse_int(_get_champ(champs, 8))
            seuil_bas = _try_parse_float(_get_champ(champs, 9))
            seuil_haut = _try_parse_float(_get_champ(champs, 10))
            prix_theorique = _try_parse_float(_get_champ(champs, 11))
            quantite_theorique = _try_parse_int(_get_champ(champs, 12))
            variation_theorique_str = _get_champ(champs, 13)
            cours_ouverture = _try_parse_float(_get_champ(champs, 14))

            # Carnet d'ordres
            nb_ordres_achat_1 = _try_parse_int(_get_champ(champs, 15))
            qte_achat_1 = _try_parse_int(_get_champ(champs, 16))
            cours_achat_1 = _parse_prix_achat_vente(_get_champ(champs, 17))
            cours_vente_1 = _parse_prix_achat_vente(_get_champ(champs, 18))
            qte_vente_1 = _try_parse_int(_get_champ(champs, 19))
            nb_ordres_vente_1 = _try_parse_int(_get_champ(champs, 20))
            # ... (Répéter pour les niveaux 2 à 5, ou créer une boucle)
            carnet_ordres_complet = []
            for i in range(5): # 5 niveaux
                offset = i * 6 # 6 champs par niveau (NbA, QA, CA, CV, QV, NbV)
                nb_a = _try_parse_int(_get_champ(champs, 15 + offset))
                q_a = _try_parse_int(_get_champ(champs, 16 + offset))
                c_a = _parse_prix_achat_vente(_get_champ(champs, 17 + offset))
                c_v = _parse_prix_achat_vente(_get_champ(champs, 18 + offset))
                q_v = _try_parse_int(_get_champ(champs, 19 + offset))
                nb_v = _try_parse_int(_get_champ(champs, 20 + offset))
                if any(v is not None for v in [nb_a, q_a, c_a, c_v, q_v, nb_v]): # Ajouter seulement si au moins une valeur existe
                    carnet_ordres_complet.append({
                        'nb_ordres_achat': nb_a, 'qte_achat': q_a, 'cours_achat': c_a,
                        'cours_vente': c_v, 'qte_vente': q_v, 'nb_ordres_vente': nb_v
                    })


            valeur_echangee = _try_parse_float(_get_champ(champs, 45).replace(' ', ''))
            valeur_cmp = _try_parse_float(_get_champ(champs, 46))
            nom_instrument = _get_champ(champs, 47)
            qte_dernier_echange = _try_parse_int(_get_champ(champs, 48)) # QtEE
            info_dernier_echange = _get_champ(champs, 49) # Dern
            statut_suspension = _get_champ(champs, 50).replace('\n', '').strip()
            horodatage_derniere_exec_complet = _get_champ(champs, 51) # ex: "09/05/2025 14:30:25"

            heure_derniere_exec = None
            if horodatage_derniere_exec_complet and ' ' in horodatage_derniere_exec_complet:
                 try: heure_derniere_exec = horodatage_derniere_exec_complet.split(' ')[1]
                 except IndexError: heure_derniere_exec = horodatage_derniere_exec_complet

            variation_montant = None
            if dernier_cours is not None and cours_veille is not None and cours_veille != 0:
                variation_montant = dernier_cours - cours_veille

            # --- Mettre à jour le cache actuel ---
            donnees_instrument_actuel = {
                'mnemonique': mnemonique, 'code_isin': code_isin, 'groupe_marche': groupe_marche,
                'nom': nom_instrument, 'cours_veille': cours_veille, 'cours_ouverture': cours_ouverture,
                'cours_max_jour': cours_max_jour, 'cours_min_jour': cours_min_jour, 'dernier_cours': dernier_cours,
                'variation_pourcentage': variation_pct, 'variation_montant': variation_montant,
                'quantite_echangee': quantite_echangee, 'valeur_echangee': valeur_echangee,
                'seuil_bas': seuil_bas, 'seuil_haut': seuil_haut,
                'prix_theorique_ouverture': prix_theorique, 'quantite_theorique_ouverture': quantite_theorique,
                'variation_theorique_ouverture': variation_theorique_str,
                'carnet_ordres': carnet_ordres_complet, # Stocker le carnet complet
                'valeur_cmp': valeur_cmp, 'quantite_dernier_echange': qte_dernier_echange,
                'info_dernier_echange': info_dernier_echange, 'statut_suspension': statut_suspension,
                'heure_derniere_execution': heure_derniere_exec,
                'horodatage_derniere_execution': horodatage_derniere_exec_complet
            }
            cache_actuel[mnemonique] = donnees_instrument_actuel

            # --- Clé pour l'historique : horodatage de l'exécution ou du message ---
            cle_horodatage = horodatage_derniere_exec_complet
            if not cle_horodatage: # Fallback si pas d'heure d'exécution spécifique
                cle_horodatage = cache_actuel.get('_metadata_horodatage_message', datetime.now().isoformat(sep=' ', timespec='seconds'))

            # --- 1. Historique Profondeur Marché ---
            if mnemonique not in hist_prof:
                hist_prof[mnemonique] = {}
                # On ne stocke que le carnet d'ordres et le timestamp de la source
                hist_prof[mnemonique][cle_horodatage] = {
                    'carnet_ordres': carnet_ordres_complet,
                    'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')
                }
            else :
                hist_prof[mnemonique][cle_horodatage] = {
                    'carnet_ordres': carnet_ordres_complet,
                    'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')}


            # --- 2. Historique Variation Marché ---
            if mnemonique not in hist_var:
                hist_var[mnemonique] = {}
                hist_var[mnemonique][cle_horodatage] = {
                    'dernier_cours': dernier_cours,
                    'variation_pourcentage': variation_pct,
                    'variation_montant': variation_montant,
                    'quantite_echangee_jour': quantite_echangee, # Volume total du jour
                    'valeur_echangee_jour': valeur_echangee,   # Valeur totale du jour
                    'cours_max_jour': cours_max_jour,
                    'cours_min_jour': cours_min_jour,
                    'cours_ouverture': cours_ouverture,
                    'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')
                }
            else :
                hist_var[mnemonique][cle_horodatage] = {
                    'dernier_cours': dernier_cours,
                    'variation_pourcentage': variation_pct,
                    'variation_montant': variation_montant,
                    'quantite_echangee_jour': quantite_echangee, # Volume total du jour
                    'valeur_echangee_jour': valeur_echangee,   # Valeur totale du jour
                    'cours_max_jour': cours_max_jour,
                    'cours_min_jour': cours_min_jour,
                    'cours_ouverture': cours_ouverture,
                    'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')
                }

            # --- 3. Historique Transactions (interprété comme la dernière transaction du snapshot) ---
            if qte_dernier_echange is not None and dernier_cours is not None : # Si une transaction semble avoir eu lieu
                if mnemonique not in hist_trans:
                    hist_trans[mnemonique] = {}
                    hist_trans[mnemonique][cle_horodatage] = {
                        'cours_transaction': dernier_cours, # Le dernier cours est le cours de la "dernière transaction" dans ce snapshot
                        'quantite_transaction': qte_dernier_echange, # QtEE
                        'info_transaction': info_dernier_echange, # Dern
                        'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')
                    }
                    maj_effectuee = True
                else:
                    hist_trans[mnemonique][cle_horodatage] = {
                        'cours_transaction': dernier_cours, # Le dernier cours est le cours de la "dernière transaction" dans ce snapshot
                        'quantite_transaction': qte_dernier_echange, # QtEE
                        'info_transaction': info_dernier_echange, # Dern
                        'horodatage_source_message': cache_actuel.get('_metadata_horodatage_message')
                    }
                    maj_effectuee = True


        # --- Gestion des métadonnées (pour le cache actuel) ---
        elif pac_det_chaine.startswith("~finresm/"):
            valeur = pac_det_chaine.split('/', 1)[1]
            cache_actuel['_metadata_finresm'] = _try_parse_float(valeur)
            maj_effectuee = True
        elif pac_det_chaine.startswith("~CT/"):
            valeur = pac_det_chaine.split('/', 1)[1]
            cache_actuel['_metadata_horodatage_message'] = valeur
            maj_effectuee = True
        elif pac_det_chaine.startswith("~VA/"):
            valeur = pac_det_chaine.split('/', 1)[1]
            cache_actuel['_metadata_valeur_agregee_va'] = _try_parse_int(valeur)
            maj_effectuee = True
        else:
             pass
    except Exception as e:
        print(f"ERREUR: Erreur de parsing sur la ligne: '{pac_det_chaine}' - {type(e).__name__}: {e}")
        # import traceback; traceback.print_exc() # Pour un débogage plus poussé
        return False
    return maj_effectuee

def traiter_bloc_xml(chaine_xml, cache_actuel, hist_prof, hist_var, hist_trans):
    try:
        if isinstance(chaine_xml, str): chaine_xml_bytes = chaine_xml.encode('utf-8')
        else: chaine_xml_bytes = chaine_xml
        racine = ET.fromstring(chaine_xml_bytes)
        type_msg_elem = racine.find('TYPE')
        if racine.tag != 'REP' or type_msg_elem is None or type_msg_elem.text != 'MKT':
            print(f"AVERT: Format XML racine ou TYPE inattendu.")
            return False
        pacq = racine.find('PACQ')
        if pacq is None:
            print("AVERT: Balise PACQ non trouvée.")
            return False
        maj_effectuees = 0
        for pac_det in pacq.findall('PAC_DET'):
            if pac_det.text:
                if parse_et_maj_caches_specialises(pac_det.text, cache_actuel, hist_prof, hist_var, hist_trans):
                    maj_effectuees += 1
        # print(f"--- Bloc XML traité. {maj_effectuees} mises à jour détectées. Horodatage global: {cache_actuel.get('_metadata_horodatage_message', 'N/A')} ---")
        return True
    except ET.ParseError as e: print(f"ERREUR: Erreur de parsing XML: {e}"); return False
    except Exception as e: print(f"ERREUR: Erreur inattendue: {e}"); return False
