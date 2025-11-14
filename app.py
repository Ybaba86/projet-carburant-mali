import streamlit as st
from supabase import create_client, Client
from streamlit_folium import st_folium
import folium
import logging
from datetime import datetime, timedelta
from twilio.rest import Client as TwilioClient
from streamlit_autorefresh import st_autorefresh
import bcrypt

# --- 0. Configuration de la Page ---
st.set_page_config(page_title="Gestion Essence Bamako", layout="wide")
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
    """Initialise la connexion à Twilio."""
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

@st.cache_data(ttl=15)
def get_stations():
    """
    Récupère la liste des stations ET LE COMPTAGE de leur file
    en appelant la fonction SQL (RPC) de Supabase.
    """
    try:
        # Appelle la fonction SQL 'get_stations_with_queue_counts'
        response = supabase.rpc('get_stations_with_queue_counts', {}).execute()
        return response.data
    except Exception as e:
        st.error(f"Erreur lors de la récupération des stations : {e}")
        return []

def register_client(identifiant_vehicule, telephone_client, station_id):
    """Tente d'inscrire un client."""
    try:
        # --- VÉRIFICATION : RÈGLE DES 2 JOURS ---
        date_limite = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        
        response_history = supabase.table("historiqueservices") \
            .select("service_id", count='exact', head=True) \
            .eq("identifiant_vehicule", identifiant_vehicule) \
            .gte("date_service", date_limite) \
            .execute()

        if response_history.count > 0:
            return (False, "Erreur : Ce véhicule a déjà été servi dans les 2 derniers jours et ne peut pas se réinscrire.")
        # --- FIN DE LA VÉRIFICATION ---

        # Si la vérification passe, continuer l'inscription
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
    """Récupère le statut d'un client."""
    try:
        response = supabase.table("fileattente") \
            .select("station_id, heure_inscription, statut, stations(nom_station)") \
            .eq("identifiant_vehicule", identifiant_vehicule) \
            .in_("statut", ["en_attente", "notifie"]) \
            .execute()
        
        if not response.data:
            return None, "Vous n'êtes actuellement dans aucune file d'attente active."

        user_entry = response.data[0]
        station_id = user_entry['station_id']
        user_time = user_entry['heure_inscription']
        user_status = user_entry['statut']
        station_name = user_entry['stations']['nom_station'] if user_entry.get('stations') else "Inconnue"

        response_list = supabase.table("fileattente") \
            .select("file_id") \
            .eq("station_id", station_id) \
            .in_("statut", ["en_attente", "notifie"]) \
            .lt("heure_inscription", user_time) \
            .execute()

        position = len(response_list.data)
        
        return {"station": station_name, "statut": user_status, "position": position}, None

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

@st.cache_data(ttl=15)
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

def mark_as_served(file_id, identifiant_vehicule, station_id):
    """Passe un client au statut 'servi' et l'ajoute à l'historique."""
    try:
        supabase.table("fileattente") \
            .update({"statut": "servi"}) \
            .eq("file_id", file_id) \
            .execute()
        
        supabase.table("historiqueservices").insert({
            "identifiant_vehicule": identifiant_vehicule,
            "station_id": station_id
        }).execute()
        
        logging.info(f"Client {identifiant_vehicule} marqué comme 'servi'.")
        return True
    except Exception as e:
        st.error(f"Erreur lors de la mise à jour 'servi': {e}")
        return False

# --- 3. Définition des Pages ---

def client_page(stations_data):
    """Affiche la page principale pour les clients."""
    st_autorefresh(interval=600000, key="client_refresh")
    
    st.title("⛽ Gestion des files d'attente (Essence - Bamako)")
    
    st.header("Localisez une station")
    if stations_data:
        map_center = [12.6392, -8.0029]
        m = folium.Map(location=map_center, zoom_start=12)
        for station in stations_data:
            couleur = "green" if station['carburant_disponible'] else "red"
            queue_count = station.get('queue_count', 0)
            popup_text = f"""
            <strong>{station['nom_station']}</strong><br>
            Disponible: {'Oui' if station['carburant_disponible'] else 'Non'}<br>
            File d'attente: {queue_count} personne(s)
            """
            folium.Marker(
                [station['latitude'], station['longitude']],
                popup=popup_text,
                tooltip=f"{station['nom_station']} (File: {queue_count})",
                icon=folium.Icon(color=couleur, icon="gas-pump", prefix='fa')
            ).add_to(m)
        st_folium(m, width=725, height=500)
    else:
        st.warning("Aucune station n'a été trouvée dans la base de données.")

    st.header("🎟️ S'inscrire à une file d'attente")
    if stations_data:
        station_options = {}
        for s in stations_data:
            if s['carburant_disponible']:
                queue_count = s.get('queue_count', 0)
                display_name = f"{s['nom_station']} (File: {queue_count} personne(s))"
                station_options[display_name] = s['station_id']

        if not station_options:
            st.warning("Aucune station n'a de carburant disponible pour le moment.")
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
                        if success: st.success(message)
                        else: st.error(message)

    st.header("🔍 Consulter mon statut")
    status_identifiant_raw = st.text_input("Entrez votre N° de plaque/cadre pour voir votre statut:", key="status_check_input")
    
    if st.button("Vérifier mon statut"):
        status_identifiant = status_identifiant_raw.upper()
        if not status_identifiant:
            st.warning("Veuillez entrer un identifiant.")
        else:
            with st.spinner("Recherche de votre position..."):
                status_info, error = get_client_status(status_identifiant)
            if error:
                st.info(error)
            elif status_info:
                st.success(f"**Station :** {status_info['station']}")
                st.write(f"**Votre statut :** {status_info['statut'].capitalize()}")
                st.write(f"**Personnes devant vous :** {status_info['position']}")
                # --- CORRECTION DE LA FAUTE DE FRAPPE ---
                if status_info['statut'] == 'notifie':
                    st.info("🔔 Vous avez été notifié ! Veuillez vous rendre à la station-service.")

def pompiste_page(stations_data):
    """Affiche la page de gestion pour le pompiste."""
    st_autorefresh(interval=120000, key="pompiste_refresh")
    
    st.title("🧑‍💼 Interface Pompiste")
    
    if 'pompiste_logged_in' not in st.session_state:
        st.session_state['pompiste_logged_in'] = False
        st.session_state['station_id'] = None
        st.session_state['station_name'] = None
        st.session_state['monitoring_active'] = False

    def reset_monitoring():
        st.session_state['monitoring_active'] = False

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
                st.session_state['monitoring_active'] = False
                st.rerun()
            else:
                st.error("Nom d'utilisateur ou mot de passe incorrect.")
        
        return
    
    selected_station_id = st.session_state['station_id']
    selected_station_name = st.session_state['station_name']

    st.success(f"Connecté en tant que: {selected_station_name}")
    
    if st.button("Se déconnecter", type="primary"):
        st.session_state['pompiste_logged_in'] = False
        st.session_state['station_id'] = None
        st.session_state['station_name'] = None
        st.session_state['monitoring_active'] = False
        st.rerun()

    st.header(f"Gestion de la station: {selected_station_name}")
    
    if st.session_state['monitoring_active']:
        st.info("🟢 Monitoring auto-remplissage : ACTIF", icon="🤖")
    else:
        st.info("🔴 Monitoring auto-remplissage : INACTIF. (Cliquez sur 'Appeler' ou 'Servi' pour l'activer)", icon="💤")
    
    col_btn1, col_btn2 = st.columns([1,2])
    with col_btn1:
        if st.button("Rafraîchir (Manuel)"):
            get_queue_for_station.clear()
            st.rerun()
            
    with col_btn2:
        num_to_call = st.selectbox(
            "Nombre de clients à appeler :",
            options=[1, 3, 5, 10],
            index=0
        )
        
        if st.button(f"Appeler {num_to_call} client(s) de la file virtuelle"):
            with st.spinner("Appel des clients suivants..."):
                update_physical_queue(selected_station_id, selected_station_name, num_to_call)
                st.session_state['monitoring_active'] = True
            get_queue_for_station.clear() 
            st.rerun()

    file_physique, file_virtuelle = get_queue_for_station(selected_station_id)
    
    if st.session_state['monitoring_active']:
        places_libres = 10 - len(file_physique)
        if places_libres > 0 and len(file_virtuelle) > 0:
            with st.spinner("Remplissage automatique d'une place..."):
                logging.info("Auto-refresh: Remplissage automatique d'une place.")
                update_physical_queue(selected_station_id, selected_station_name, num_to_call=1) 
                get_queue_for_station.clear()
                st.rerun()
    
    col_file1, col_file2 = st.columns(2)
    
    with col_file1:
        st.subheader(f"File Physique (Notifiés) : {len(file_physique)} / 10")
        if not file_physique:
            st.info("La file physique est vide.")
        else:
            with st.container(height=400):
                for i, client in enumerate(file_physique):
                    key = f"servi_btn_{i}"
                    if st.button(f"Marquer '{client['identifiant_vehicule']}' comme Servi", key=key):
                        
                        with st.spinner("Mise à jour..."):
                            success = mark_as_served(
                                client['file_id'], 
                                client['identifiant_vehicule'], 
                                selected_station_id
                            )
                        
                        if success:
                            st.success(f"Client {client['identifiant_vehicule']} marqué comme servi.")
                            st.session_state['monitoring_active'] = True
                            update_physical_queue(selected_station_id, selected_station_name, num_to_call=1)
                            get_queue_for_station.clear() 
                            st.rerun()

    with col_file2:
        st.subheader(f"File Virtuelle (En attente) : {len(file_virtuelle)}")
        if not file_virtuelle:
            st.info("La file virtuelle est vide.")
        else:
            with st.container(height=400):
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
    st.info("Créez ou mettez à jour le nom d'utilisateur et le mot de passe pour une station.")

    if not stations_data:
        st.warning("Aucune station à configurer.")
        return

    # 1. Dictionnaire pour la sélection {NomStation: StationObjet}
    station_options = {s['nom_station']: s for s in stations_data}
    
    # 2. Sélecteur de station
    selected_station_name = st.selectbox(
        "Sélectionnez une station à modifier:",
        options=list(station_options.keys())
    )

    # 3. Afficher le formulaire UNIQUEMENT pour la station sélectionnée
    if selected_station_name:
        selected_station = station_options[selected_station_name]
        station_id = selected_station['station_id']
        current_username = selected_station.get('pompiste_username', "")
        
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
            
            submit_button = st.form_submit_button("Mettre à jour")

            if submit_button:
                if not new_username:
                    st.error("Le nom d'utilisateur ne peut pas être vide.")
                else:
                    try:
                        update_data = {
                            "pompiste_username": new_username
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
                        get_stations.clear()
                        st.rerun()

                    except Exception as e:
                        st.error(f"Erreur lors de la mise à jour: {e}")

# --- 4. Routeur Principal ---
def main():
    """Routeur principal pour naviguer entre les pages."""
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