from werkzeug.exceptions import abort

from .config import Configuration
from .user import User
from .wrappers import is_admin, requires_admin, check_db_conn

from flask import render_template, request, redirect, flash, url_for, Blueprint
from flask_login import login_user, logout_user, login_required, current_user

blob_api = Blueprint('blob_api', __name__)


def get_cracked_tuple(handshake, document):
    ssid = handshake["SSID"]
    mac = handshake["MAC"]
    hs_type = handshake["handshake_type"]
    date_added = document["date_added"].strftime('%H:%M - %d.%m.%Y')
    cracked_by = handshake["cracked_rule"]

    password = handshake["password"]
    date = handshake["date_cracked"].strftime('%H:%M - %d.%m.%Y')
    raw_date = handshake["date_cracked"]

    return ssid, mac, hs_type, date_added, cracked_by, password, date, raw_date


def get_uncracked_tuple(handshake, document, reserved_data):
    ssid = handshake["SSID"]
    mac = handshake["MAC"]
    hs_type = handshake["handshake_type"]
    date_added = document["date_added"].strftime('%H:%M - %d.%m.%Y')
    if handshake["active"]:
        tried_rules = "Trying rule %s" % reserved_data["tried_rule"]
        eta = handshake["eta"]
    else:
        tried_rules = "%s/%s" % (len(handshake["tried_dicts"]), Configuration.number_rules)
        eta = ""

    return ssid, mac, hs_type, date_added, tried_rules, eta


@blob_api.route('/admin/', methods=['GET', 'POST'])
@requires_admin
def admin_panel():
    if request.method == 'GET':
        if check_db_conn() is None:
            flash("DATABASE IS DOWN!")
            return render_template('admin.html')

        admin_table, error = Configuration.get_admin_table()
        if admin_table is None:
            flash(error)
            return render_template('admin.html')

        workload = int(admin_table["workload"])

        if workload < 1 or workload > 4:
            workload = 2
            flash("Workload returned by database is not within bounds! Corrected to value 2.")
            Configuration.logger.error("Workload returned by database is not within bounds! Corrected to value 2.")

        return render_template('admin.html', workload=workload)

    elif request.method == 'POST':
        workload = int(request.form.get("workload", None))
        force = False if request.form.get("force_checkbox", None) is None else True

        update = {"workload": workload, "force": force}

        flash("Workload = '%s', force = '%s'" % (workload, force), category='success')

        Configuration.set_admin_table(update)

        return render_template('admin.html', workload=workload)
    else:
        Configuration.logger.error("Unsupported method!")
        abort(404)


@blob_api.route('/', methods=['GET'])
def home():
    if is_admin(current_user):
        if check_db_conn() is None:
            flash("DATABASE IS DOWN")
            return render_template('admin_home.html')

        # Dictionary with key=<user>, value=[<handshake>]
        user_handshakes = {}

        try:
            all_files = Configuration.wifis.find({}).sort([("date_added", 1)])
            Configuration.logger.info("Retrieved all user data for admin display.")
        except Exception as e:
            Configuration.logger.error("Database error at retrieving all user data %s" % e)
            flash("Database error at retrieving all user data %s" % e)
            return render_template('admin_home.html')

        for file_structure in all_files:
            # First user is the original uploader
            crt_user = file_structure["users"][0]

            if crt_user not in user_handshakes:
                user_handshakes[crt_user] = [[], []]

            handshake = file_structure["handshake"]
            if handshake["password"] == "":
                user_handshakes[crt_user][0].append(get_uncracked_tuple(handshake, file_structure,
                                                                        file_structure["reserved"]))
            else:
                user_handshakes[crt_user][1].append(get_cracked_tuple(handshake, file_structure))

        # Sort based on crack date and remove trailing raw date
        for entry in user_handshakes.values():
            entry[1] = sorted(entry[1], key=lambda k: k[7])

        # Transform dict to list and sort by username
        user_handshakes = sorted(user_handshakes.items(), key=lambda k: k[0])

        return render_template('admin_home.html', user_handshakes=user_handshakes)

    logged_in = current_user.is_authenticated
    if logged_in and check_db_conn() is None:
        flash("Database error!")
        return render_template('home.html', logged_in=True)

    uncracked = []
    cracked = []
    if logged_in:
        # Sort in mongo by the time the handshake was added
        for file_structure in Configuration.wifis.find({"users": current_user.get_id()}).sort([("date_added", 1)]):
            # Sort in python by the SSID
            handshake = file_structure["handshake"]
            if handshake["password"] == "":
                uncracked.append(get_uncracked_tuple(handshake, file_structure, file_structure["reserved"]))
            else:
                cracked.append(get_cracked_tuple(handshake, file_structure))

    # Sort based on crack date and remove trailing raw date
    cracked = sorted(cracked, key=lambda k: k[7])

    return render_template('home.html', uncracked=uncracked, cracked=cracked, logged_in=logged_in)


def get_rule_tuple(rule):
    try:
        priority = rule["priority"]
        name = rule["name"]
    except KeyError:
        Configuration.logger.error("Error! Malformed rule %s" % rule)
        return None

    examples = ""
    desc = ""
    link = ""
    try:
        desc = rule["desc"]
        link = rule["link"]
        for example in rule["examples"]:
            examples += example + " "

        if len(examples) > 0:
            examples = examples[:-1]
    except KeyError:
        pass

    return priority, name, desc, examples, link


@blob_api.route('/statuses/', methods=['GET'])
@login_required
def statuses():
    status_list = []

    for rule in Configuration.get_active_rules():
        status_list.append(get_rule_tuple(rule))

    return render_template('statuses.html', statuses=status_list)


@blob_api.route('/login/', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        flash("User is already authenticated!")
        return redirect(url_for("blob_api.home"))

    if request.method == 'GET':
        return render_template('login.html')

    if request.method == 'POST':
        username = request.form.get("username", None)
        password = request.form.get("password", None)

        if username is None or len(username) == 0:
            flash("No username introduced!")
            return redirect(request.url)

        if password is None or len(password) == 0:
            flash("No password introduced!")
            return redirect(request.url)

        Configuration.logger.info("Login attempt from username = '%s'" % username)

        if not User.check_credentials(username, password):
            flash("Incorrect username/password!")
            return redirect(request.url)

        login_user(User(username))

        return redirect(url_for("blob_api.home"))


@blob_api.route("/register/", methods=["GET", "POST"])
def register():
    if request.method == 'GET':
        if current_user.is_authenticated:
            flash("You are already have an account")
            return redirect(url_for("blob_api.home"))
        return render_template('register.html')

    if request.method == "POST":
        username = request.form.get("username", None)
        password = request.form.get("password", None)

        if username is None or len(username) == 0:
            flash("No username introduced!")
            return redirect(request.url)

        if password is None or len(password) == 0:
            flash("No password introduced!")
            return redirect(request.url)

        if len(password) < 6:
            flash("C'mon... use at least 6 characters... pretty please?")
            return redirect(request.url)

        if Configuration.username_regex.search(username) is None:
            flash("Username should start with a letter and only contain alphanumeric or '-._' characters!")
            return redirect(request.url)

        if len(username) > 150 or len(password) > 150:
            flash("Either the username or the password is waaaaay too long. Please dont.")
            return redirect(request.url)

        retval = User.create_user(username, password)

        if retval is None:
            return redirect(url_for("blob_api.home"))

        flash(retval)
        return redirect(request.url)


@blob_api.route('/logout/', methods=["GET"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("blob_api.home"))
