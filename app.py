import streamlit as st
from supabase import create_client, Client
from streamlit_folium import st_folium
import folium
import logging
from datetime import datetime, timedelta
from twilio.rest import Client as TwilioClient
from streamlit_autorefresh import st_autorefresh # Importé pour le rafraîchissement
import bcrypt

# --- 0. Configuration de la Page ---
st.set_page_config(page_title="Gestion Carburant Mali", layout="wide") # <-- Titre de l'onglet modifié
logging.basicConfig(level=logging.INFO)

# --- 1. Connexion à Supabase & Twilio ---
@st.cache_resource
def init_connection():
    """Initialise la connexion à Supabase."""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase: Client = init_connection()

# Cache pour le client Twilio
@st.cache_resource
def init_twilio_client():
    """Initialise le client Twilio."""
    try:
        account_sid = st.secrets["twilio"]["account_sid"]
        auth_token = st.secrets["twilio"]["auth_token"]
        return TwilioClient(account_sid, auth_token)
    except Exception as e:
        logging.error(f"Erreur init Twilio: {e}")
        return None

twilio_client = init_twilio_client()
TWILIO_PHONE_NUMBER = st.secrets["twilio"].get("phone_number")

# --- 2. Fonctions de la Base de Données ---

# --- MODIFIÉ : Cache @st.cache_data(ttl=15) SUPPRIMÉ ---
def get_stations():
    """
    Récupère la liste des stations ET LE COMPTAGE de leur file
    en appelant la fonction SQL (RPC) de Supabase.
    """
    try:
        response = supabase.rpc('get_stations_with_queue_counts', {}).execute()
        return response.data
    except Exception as e:
        st.error(f"Erreur lors de la récupération des stations : {e}")
        return []

def register_client(identifiant_vehicule, telephone_client, station_id):
    """Tente d'inscrire un client."""
    try:
        # Vérification 1: Règle des 2 jours
        date_limite = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        
        response_history = supabase.table("historiqueservices") \
            .select("service_id", count='exact', head=True) \
            .eq("identifiant_vehicule", identifiant_vehicule) \
            .gte("date_service", date_limite) \
            .execute()

        if response_history.count > 0:
            return (False, "Erreur : Ce véhicule a déjà été servi dans les 2 derniers jours et ne peut pas se réinscrire.")

        # Vérification 2: Inscription (gérée par la BDD avec la contrainte)
        supabase.table("vehicules").upsert({
            "identifiant_vehicule": identifiant_vehicule,
            "telephone_client": telephone_client
        }).execute()

        supabase.table("fileattente").insert({
            "station_id": station_id,
            "identifiant_vehicule": identifiant_vehicule,
            "statut": "en_attente"
        }).execute()
        
        return (True, "Inscription à la file d'attente réussie !")

    except Exception as e:
        error_message = str(e)
        if "uq_vehicule_en_attente_partial" in error_message or "duplicate key" in error_message:
            return (False, "Erreur : Ce véhicule est déjà dans une file d'attente active.")
        else:
            logging.error(f"Erreur inscription: {error_message}")
            return (False, "Erreur : Impossible de traiter l'inscription.")

def get_client_status(identifiant_vehicule):
    """Récupère le statut d'un client ET LE STOCK DE LA STATION."""
    try:
        response = supabase.table("fileattente") \
            .select("station_id, heure_inscription, statut, stations(nom_station, stock_estime)") \
            .eq("identifiant_vehicule", identifiant_vehicule) \
            .in_("statut", ["en_attente", "notifie"]) \
            .execute()
        
        if not response.data:
            return None, "Vous n'êtes actuellement dans aucune file d'attente active."

        user_entry = response.data[0]
        station_id = user_entry['station_id']
        user_time = user_entry['heure_inscription']
        user_status = user_entry['statut']
        station_name = "Inconnue"
        stock_estime = 0
        
        if user_entry.get('stations'):
            station_name = user_entry['stations'].get('nom_station', 'Inconnue')
            stock_estime = user_entry['stations'].get('stock_estime', 0)

        # Compter les gens avant
        response_list = supabase.table("fileattente") \
            .select("file_id") \
            .eq("station_id", station_id) \
            .in_("statut", ["en_attente", "notifie"]) \
            .lt("heure_inscription", user_time) \
            .execute()

        position = len(response_list.data)
        
        return {"station": station_name, "statut": user_status, "position": position, "stock": stock_estime}, None

    except Exception as e:
        logging.error(f"Erreur statut: {e}")
        return None, "Une erreur est survenue en consultant votre statut."


def send_sms(to_number, body_message):
    """Envoie un SMS via Twilio, en forçant le préfixe +223 si manquant."""
    if not twilio_client or not TWILIO_PHONE_NUMBER:
        logging.warning("Configuration Twilio manquante. SMS non envoyé.")
        st.warning("SMS non configuré sur le serveur.")
        return False
        
    try:
        formatted_to_number = str(to_number).strip().replace(" ", "") # Nettoyer
        
        if not formatted_to_number.startswith('+'):
            logging.info(f"Numéro {formatted_to_number} n'est pas au format E.164, ajout du préfixe +223.")
            formatted_to_number = f"+223{formatted_to_number}"

        message = twilio_client.messages.create(
            body=body_message,
            from_=TWILIO_PHONE_NUMBER,
            to=formatted_to_number
        )
        logging.info(f"SMS envoyé à {to_number}, SID: {message.sid}")
        return True
    except Exception as e:
        logging.error(f"Erreur envoi SMS à {to_number}: {e}")
        st.error(f"Échec de l'envoi du SMS à {to_number}. (Erreur Twilio: {e})")
        return False

# --- Fonctions Pompiste ---

# --- MODIFIÉ : Cache @st.cache_data(ttl=15) SUPPRIMÉ ---
def get_queue_for_station(station_id):
    """Récupère les files 'notifie' (physique) et 'en_attente' (virtuelle) pour une station."""
    try:
        response_notifie = supabase.table("fileattente") \
            .select("file_id, identifiant_vehicule, heure_inscription") \
            .eq("station_id", station_id) \
            .eq("statut", "notifie") \
            .order("heure_inscription", desc=False) \
            .execute()

        response_en_attente = supabase.table("fileattente") \
            .select("file_id, identifiant_vehicule, heure_inscription") \
            .eq("station_id", station_id) \
            .eq("statut", "en_attente") \
            .order("heure_inscription", desc=False) \
            .execute()
            
        return response_notifie.data, response_en_attente.data
    except Exception as e:
        st.error(f"Erreur récupération files: {e}")
        return [], []

def update_physical_queue(station_id, station_name, num_to_call, max_queue_size=10):
    """
    Met à jour la file physique en appelant 'num_to_call' clients,
    sans dépasser 'max_queue_size'.
    """
    try:
        # 1. Compter la file physique actuelle
        response_count = supabase.table("fileattente") \
            .select("count", head=True) \
            .eq("station_id", station_id) \
            .eq("statut", "notifie") \
            .execute()
        
        current_queue_size = response_count.count if response_count.count is not None else 0
        
        # 2. Calculer les places libres et le nombre réel à appeler
        places_libres = max_queue_size - current_queue_size
        actual_num_to_call = min(places_libres, num_to_call)
        
        logging.info(f"File physique: {current_queue_size}/{max_queue_size}. Places libres: {places_libres}. Demande d'appel: {num_to_call}. Appel réel: {actual_num_to_call}")

        if actual_num_to_call > 0:
            # 3. ...trouver les N prochains clients en attente
            response_next = supabase.table("fileattente") \
                .select("file_id, identifiant_vehicule, vehicules(telephone_client)") \
                .eq("station_id", station_id) \
                .eq("statut", "en_attente") \
                .order("heure_inscription", desc=False) \
                .limit(actual_num_to_call) \
                .execute()
            
            if response_next.data:
                clients_a_notifier = response_next.data
                client_ids = [client['file_id'] for client in clients_a_notifier]
                
                # 4. Changer leur statut à 'notifie'
                supabase.table("fileattente") \
                    .update({"statut": "notifie"}) \
                    .in_("file_id", client_ids) \
                    .execute()
                
                sms_envoyes = 0
                for client in clients_a_notifier:
                    try:
                        to_number = client['vehicules']['telephone_client']
                        message = f"Gestion Essence: C'est votre tour ! Veuillez vous rendre à la {station_name}."
                        if send_sms(to_number, message):
                            sms_envoyes += 1
                    except Exception as e:
                        logging.error(f"Erreur extraction N° tel pour {client['identifiant_vehicule']}: {e}")

                logging.info(f"{len(clients_a_notifier)} client(s) notifié(s). SMS envoyés: {sms_envoyes}")
                st.success(f"{len(clients_a_notifier)} client(s) ont été notifié(s) !")
            else:
                st.info("Aucun client dans la file virtuelle à appeler.")
        else:
            if places_libres <= 0:
                st.warning("La file physique est déjà pleine (10/10).")
            else:
                st.info("Veuillez sélectionner au moins 1 client à appeler.")

    except Exception as e:
        logging.error(f"Erreur lors de la mise à jour de la file physique: {e}")

def mark_as_served(file_id, identifiant_vehicule, station_id, litres_vendus):
    """Passe un client au statut 'servi', ajoute à l'historique ET DÉCRÉMENTE LE STOCK."""
    try:
        # 1. Mettre à jour le statut
        supabase.table("fileattente") \
            .update({"statut": "servi"}) \
            .eq("file_id", file_id) \
            .execute()
        
        # 2. Ajouter à l'historique (AVEC les litres)
        supabase.table("historiqueservices").insert({
            "identifiant_vehicule": identifiant_vehicule,
            "station_id": station_id,
            "litres_vendus": litres_vendus # <-- Ajouté
        }).execute()
        
        # 3. Appeler la fonction RPC pour décrémenter le stock
        supabase.rpc('decrement_station_stock', {
            'p_station_id': station_id,
            'p_litres_sold': litres_vendus
        }).execute()
        
        logging.info(f"Client {identifiant_vehicule} marqué 'servi'. {litres_vendus}L déduits.")
        return True
    except Exception as e:
        st.error(f"Erreur lors de la mise à jour 'servi': {e}")
        return False

def cancel_queue_entry(file_id):
    """Appelle la fonction RPC pour annuler un client."""
    try:
        supabase.rpc('cancel_queue_entry', { 'p_file_id': file_id }).execute()
        logging.info(f"Client {file_id} marqué comme 'annule'.")
        return True
    except Exception as e:
        st.error(f"Erreur lors de l'annulation: {e}")
        return False


# --- 3. Définition des Pages ---

def client_page(stations_data):
    """Affiche la page principale pour les clients."""
    
    # --- Auto-refresh (300 000ms = 5 minutes) ---
    st_autorefresh(interval=300000, key="client_refresh")
    
    st.title("⛽ Plateforme de Gestion de Carburant")
    st.caption("Gestion durant la Crise de carburant")
    
    # --- MODIFIÉ : Afficher le toast si il est en session_state ---
    if "toast_message" in st.session_state:
        # MODIFIÉ : Ajout de duration=8000 (8 secondes)
        st.toast(st.session_state.toast_message, icon="✅", duration=8000)
        del st.session_state.toast_message # L'effacer après affichage
    # --- FIN MODIFICATION ---

    # --- Navigation par onglets pour mobile ---
    tab1, tab2 = st.tabs(["🗺️ Localiser & S'inscrire", "🔍 Mon Statut"])

    with tab1:
        st.header("Localisez une station")
        if stations_data:
            map_center = [12.6392, -8.0029]
            m = folium.Map(location=map_center, zoom_start=12)
            for station in stations_data:
                couleur = "green" if station['carburant_disponible'] else "red"
                queue_count = station.get('queue_count', 0)
                stock_estime = station.get('stock_estime', 0)
                popup_text = f"""
                <strong>{station['nom_station']}</strong><br>
                Disponible: {'Oui' if station['carburant_disponible'] else 'Non'}<br>
                File d'attente: {queue_count} personne(s)<br>
                Stock estimé: {stock_estime} L
                """
                folium.Marker(
                    [station['latitude'], station['longitude']],
                    popup=popup_text,
                    tooltip=f"{station['nom_station']} (File: {queue_count} | Stock: {stock_estime} L)",
                    icon=folium.Icon(color=couleur, icon="gas-pump", prefix='fa')
                ).add_to(m)
            st_folium(m, width=725, height=400) # Hauteur réduite pour mobile
        else:
            st.warning("Aucune station n'a été trouvée dans la base de données.")

        st.header("🎟️ S'inscrire à une file d'attente")
        if stations_data:
            station_options = {}
            for s in stations_data:
                # Vérifie le stock en plus de la disponibilité
                if s['carburant_disponible'] and s.get('stock_estime', 0) > 0:
                    queue_count = s.get('queue_count', 0)
                    stock_estime = s.get('stock_estime', 0)
                    display_name = f"{s['nom_station']} (File: {queue_count} | Stock: {stock_estime} L)"
                    station_options[display_name] = s['station_id']

            if not station_options:
                st.warning("Aucune station n'a de carburant disponible pour le moment (stock > 0).")
            else:
                with st.form("inscription_form"):
                    selected_station_name = st.selectbox(
                        'Choisissez votre station:', 
                        options=list(station_options.keys())
                    )
                    identifiant_vehicule_raw = st.text_input("N° de plaque ou de cadre", max_chars=20)
                    telephone_client = st.text_input("Votre N° de téléphone (Ex: 74749730)", max_chars=20)
                    submitted = st.form_submit_button("S'inscrire")
                    
                    if submitted:
                        identifiant_vehicule = identifiant_vehicule_raw.upper()
                        if not identifiant_vehicule or not telephone_client:
                            st.error("Veuillez remplir tous les champs.")
                        else:
                            with st.spinner("Vérification et inscription en cours..."):
                                selected_station_id = station_options[selected_station_name]
                                success, message = register_client(identifiant_vehicule, telephone_client, selected_station_id)
                            
                            if success: 
                                # Stocker le message pour l'afficher APRES le rerun
                                st.session_state.toast_message = message
                                # get_stations.clear() # <-- Ligne supprimée
                                st.rerun() # Recharger la page
                            else: 
                                # Si c'est une erreur, on l'affiche directement
                                st.error(message)

    with tab2:
        st.header("🔍 Consulter mon statut")
        
        with st.form("status_check_form"):
            status_identifiant_raw = st.text_input("Entrez votre N° de plaque/cadre pour voir votre statut:", key="status_check_input")
            submitted_status = st.form_submit_button("Vérifier mon statut")
            
            if "status_check_result" in st.session_state:
                status_info = st.session_state.status_check_result.get("info")
                error = st.session_state.status_check_result.get("error")
                if error:
                    st.info(error)
                elif status_info:
                    st.success(f"**Station :** {status_info['station']}")
                    col_stat1, col_stat2 = st.columns(2)
                    col_stat1.metric(label="Votre statut", value=status_info['statut'].capitalize())
                    col_stat2.metric(label="Personnes devant vous", value=status_info['position'])
                    st.metric(label="Stock restant à la station", value=f"{status_info['stock']} L")
                    if status_info['statut'] == 'notifie':
                        st.info("🔔 Vous avez été notifié ! Veuillez vous rendre à la station-service.")
                del st.session_state.status_check_result

            if submitted_status: 
                status_identifiant = status_identifiant_raw.upper()
                if not status_identifiant:
                    st.warning("Veuillez entrer un identifiant.")
                else:
                    with st.spinner("Recherche de votre position..."):
                        status_info, error = get_client_status(status_identifiant)
                    
                    st.session_state.status_check_result = {"info": status_info, "error": error}
                    st.rerun() 

def pompiste_page(stations_data):
    """Affiche la page de gestion pour le pompiste."""
    
    # --- Auto-refresh (120 000ms = 2 minutes) ---
    st_autorefresh(interval=120000, key="pompiste_refresh")
    
    st.title("🧑‍💼 Interface Pompiste")
    
    if 'pompiste_logged_in' not in st.session_state:
        st.session_state['pompiste_logged_in'] = False
        st.session_state['station_id'] = None
        st.session_state['station_name'] = None

    if not st.session_state['pompiste_logged_in']:
        
        with st.form("login_form"):
            username = st.text_input("Nom d'utilisateur")
            password = st.text_input("Mot de passe", type="password")
            login_button = st.form_submit_button("Se connecter")
        
        if login_button:
            if not username or not password:
                st.error("Veuillez entrer un nom d'utilisateur et un mot de passe.")
                return

            found_station = None
            for station in stations_data:
                if station.get('pompiste_username') == username:
                    stored_hash_str = station.get('pompiste_password')
                    if stored_hash_str:
                        try:
                            stored_hash_bytes = stored_hash_str.encode('utf-8')
                            entered_password_bytes = password.encode('utf-8')
                            
                            if bcrypt.checkpw(entered_password_bytes, stored_hash_bytes):
                                found_station = station
                                break
                        except Exception as e:
                            logging.error(f"Erreur Bcrypt: {e}")
                            st.error("Erreur lors de la vérification du mot de passe.")
                    
            
            if found_station:
                st.session_state['pompiste_logged_in'] = True
                st.session_state['station_id'] = found_station['station_id']
                st.session_state['station_name'] = found_station['nom_station']
                st.rerun()
            else:
                st.error("Nom d'utilisateur ou mot de passe incorrect.")
        
        return
    
    # --- SI LE POMPISTE EST CONNECTÉ ---
    selected_station_id = st.session_state['station_id']
    selected_station_name = st.session_state['station_name']

    st.success(f"Connecté en tant que: {selected_station_name}")
    
    if st.button("Se déconnecter", type="primary"):
        st.session_state['pompiste_logged_in'] = False
        st.session_state['station_id'] = None
        st.session_state['station_name'] = None
        st.rerun()

    # --- Tableau de Bord Intuitif ---
    st.header("Tableau de Bord")
    
    # Récupérer les données une seule fois
    current_station_data = next((s for s in stations_data if s['station_id'] == selected_station_id), None)
    stock = current_station_data.get('stock_estime', 0) if current_station_data else 0
    file_physique, file_virtuelle = get_queue_for_station(selected_station_id)
    
    # Afficher les métriques
    col_met1, col_met2, col_met3 = st.columns(3)
    col_met1.metric("Stock Restant", f"{int(stock)} L")
    col_met2.metric("File Physique", f"{len(file_physique)} / 10")
    col_met3.metric("File Virtuelle", f"{len(file_virtuelle)}")
    st.divider()
    
    # --- Section des Actions ---
    st.subheader("Actions Pompiste")
    col_btn1, col_btn2 = st.columns([1,2])
    with col_btn1:
        if st.button("Rafraîchir (Manuel)"):
            # get_queue_for_station.clear() # <-- Ligne supprimée
            # get_stations.clear() # <-- Ligne supprimée
            st.rerun()
            
    with col_btn2:
        num_to_call = st.selectbox(
            "Nombre de clients à appeler :",
            options=[1, 3, 5, 10],
            index=0,
            key="num_to_call_select"
        )
        
        if st.button(f"Appeler {num_to_call} client(s) de la file virtuelle"):
            with st.spinner("Appel des clients suivants..."):
                update_physical_queue(selected_station_id, selected_station_name, num_to_call)
            # get_queue_for_station.clear() # <-- Ligne supprimée
            st.rerun()
    
    st.divider()

    # --- Section des Files ---
    col_file1, col_file2 = st.columns(2)
    
    with col_file1:
        st.subheader(f"File Physique (Notifiés) : {len(file_physique)} / 10")
        with st.container(height=400):
            if not file_physique:
                st.info("La file physique est vide.")
            else:
                for i, client in enumerate(file_physique):
                    key_base = client['file_id'] 
                    st.markdown(f"**Client: {client['identifiant_vehicule']}**")
                    
                    # --- Logique conditionnelle basée sur le stock ---
                    if stock > 0:
                        # Si le stock est OK, afficher le formulaire de service
                        litres_vendus = st.number_input(
                            "Litres vendus:", 
                            min_value=1.0, 
                            max_value=max(200.0, stock), # Plafonner au stock restant
                            value=5.0,
                            step=1.0,
                            key=f"litres_{key_base}"
                        )
                        
                        if st.button(f"Marquer comme Servi", key=f"servi_btn_{key_base}"):
                            
                            litres_to_deduct = st.session_state[f"litres_{key_base}"]
                            
                            if litres_to_deduct > stock:
                                st.error(f"Erreur : Vous ne pouvez pas vendre {litres_to_deduct}L, il ne reste que {stock}L.")
                            else:
                                with st.spinner("Mise à jour..."):
                                    success = mark_as_served(
                                        client['file_id'], 
                                        client['identifiant_vehicule'], 
                                        selected_station_id,
                                        litres_to_deduct 
                                    )
                                
                                if success:
                                    st.success(f"Client {client['identifiant_vehicule']} marqué comme servi.")
                                    # Appeler 1 client pour remplacer celui qui part
                                    update_physical_queue(selected_station_id, selected_station_name, num_to_call=1)
                                    # get_queue_for_station.clear() # <-- Ligne supprimée
                                    # get_stations.clear() # <-- Ligne supprimée
                                    st.rerun()
                    else:
                        # Si le stock est à 0, afficher le bouton d'annulation
                        st.warning(f"Stock épuisé ({stock}L). Vous ne pouvez plus servir.")
                        if st.button("Annuler (Stock Épuisé)", key=f"cancel_btn_{key_base}", type="primary"):
                            with st.spinner("Annulation du client..."):
                                success = cancel_queue_entry(client['file_id'])
                                if success:
                                    st.success(f"Client {client['identifiant_vehicule']} annulé et libéré.")
                                    # get_queue_for_station.clear() # <-- Ligne supprimée
                                    # get_stations.clear() # <-- Ligne supprimée
                                    st.rerun()
                    
                    st.divider()

    with col_file2:
        st.subheader(f"File Virtuelle (En attente) : {len(file_virtuelle)}")
        with st.container(height=400):
            if not file_virtuelle:
                st.info("La file virtuelle est vide.")
            else:
                st.write("Prochains clients en attente :")
                for client in file_virtuelle:
                    st.text(client['identifiant_vehicule'])

# --- PAGE ADMIN ---
def admin_page(stations_data):
    """Affiche la page d'administration pour gérer les utilisateurs pompistes."""
    st.title("👑 Interface Administrateur")

    try:
        ADMIN_PASSWORD = st.secrets["admin"]["password"]
    except KeyError:
        st.error("Mot de passe admin non configuré dans secrets.toml.")
        return

    admin_pass = st.text_input("Mot de passe Administrateur", type="password", key="admin_pass")

    if not admin_pass:
        st.warning("Veuillez entrer le mot de passe admin.")
        return

    if admin_pass != ADMIN_PASSWORD:
        st.error("Mot de passe admin incorrect.")
        return

    st.success("Accès Administrateur autorisé.")
    st.header("Gérer les comptes Pompiste")
    st.info("Créez ou mettez à jour le nom d'utilisateur, le mot de passe et le stock pour une station.")

    if not stations_data:
        st.warning("Aucune station à configurer.")
        return

    station_options = {s['nom_station']: s for s in stations_data}
    
    selected_station_name = st.selectbox(
        "Sélectionnez une station à modifier:",
        options=list(station_options.keys())
    )

    if selected_station_name:
        selected_station = station_options[selected_station_name]
        station_id = selected_station['station_id']
        current_username = selected_station.get('pompiste_username', "")
        current_stock = selected_station.get('stock_estime', 0)
        
        st.subheader(f"Modification de : {selected_station_name}")
        
        with st.form(key=f"form_{station_id}"):
            new_username = st.text_input(
                "Nom d'utilisateur Pompiste", 
                value=current_username, 
                key=f"user_{station_id}"
            )
            new_password = st.text_input(
                "Nouveau Mot de Passe (laisser vide pour ne pas changer)", 
                type="password", 
                key=f"pass_{station_id}"
            )
            new_stock = st.number_input(
                "Stock estimé (Litres)", 
                min_value=0, 
                value=int(current_stock), 
                step=100,
                key=f"stock_{station_id}"
            )
            
            submit_button = st.form_submit_button("Mettre à jour")

            if submit_button:
                if not new_username:
                    st.error("Le nom d'utilisateur ne peut pas être vide.")
                else:
                    try:
                        # --- MODIFIÉ : Mettre à jour la disponibilité avec le stock ---
                        update_data = {
                            "pompiste_username": new_username,
                            "stock_estime": new_stock,
                            "carburant_disponible": (new_stock > 0) # Vrai si stock > 0
                        }
                        
                        if new_password:
                            st.spinner("Hachage du mot de passe...")
                            salt = bcrypt.gensalt()
                            hashed_password_bytes = bcrypt.hashpw(new_password.encode('utf-8'), salt)
                            update_data["pompiste_password"] = hashed_password_bytes.decode('utf-8')
                            logging.info(f"Nouveau hachage créé pour {new_username}")

                        supabase.table("stations") \
                            .update(update_data) \
                            .eq("station_id", station_id) \
                            .execute()
                        
                        st.success(f"Informations pour {selected_station_name} mises à jour !")
                        # get_stations.clear() # <-- Ligne supprimée
                        st.rerun()

                    except Exception as e:
                        st.error(f"Erreur lors de la mise à jour: {e}")

# --- 4. Routeur Principal ---
def main():
    """Routeur principal pour naviguer entre les pages."""
    
    # --- CSS CORRIGÉ (Version "Agressive" + "wrap text" + "font-size") ---
    st.markdown("""
        <style>
            /* --- Réduire padding du haut --- */
            div.block-container {
                padding-top: 1rem !important;
            }
            [data-testid="stAppViewContainer"] > section {
                padding-top: 1rem !important;
            }
            [data-testid="stAppViewContainer"] > section:first-child {
                padding-top: 0rem !important;
            }
            [data-testid="main-content"] {
                padding-top: 1rem !important;
            }

            /* --- Forcer le retour à la ligne (wrap) dans les selectbox --- */
            
            /* Cible le texte de l'élément sélectionné */
            [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
                white-space: normal !important; /* Retour à la ligne */
                overflow-wrap: break-word !important; /* Coupe le mot si nécessaire */
                word-break: break-all !important; /* Coupe n'importe où */
                font-size: 0.85rem !important; /* --- RÉDUIRE LA POLICE --- */
            }
            
            /* Cible les options dans la liste déroulante */
            div[data-baseweb="popover"] ul li {
                white-space: normal !important; /* Retour à la ligne */
                overflow-wrap: break-word !important; /* Coupe le mot si nécessaire */
                word-break: break-all !important; /* Coupe n'importe où */
                font-size: 0.85rem !important; /* --- RÉDUIRE LA POLICE --- */
            }
        </style>
        """, unsafe_allow_html=True)
    # --- FIN DU CSS ---

    stations = get_stations()
    
    page = st.query_params.get("page", "client")

    if page == "pompiste":
        pompiste_page(stations)
    elif page == "admin": 
        admin_page(stations)
    else:
        client_page(stations)

if __name__ == "__main__":
    main()
