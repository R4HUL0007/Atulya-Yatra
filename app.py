from flask import Flask, jsonify, render_template,session, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, IntegerField, SubmitField
from wtforms.validators import DataRequired, Length, Email
from datetime import datetime
import json

# App Configuration
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///tourism.db'
app.config['SECRET_KEY'] = 'your_secret_key'
db = SQLAlchemy(app)

# Load places from JSON file
with open('data/places.json') as f:
    places = json.load(f)


# Database Models
class Trip(db.Model):
    __tablename__ = 'trips'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    itinerary_id = db.Column(db.Integer, db.ForeignKey('public_itinerary.id'), nullable=False)
    itinerary = db.relationship('PublicItinerary', backref=db.backref('trip', uselist=False))


class Place(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    state = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)
    image = db.Column(db.String(100), nullable=False)
    safety_guidelines = db.Column(db.Text, nullable=True)
    local_language = db.Column(db.Text, nullable=True)
    nearby_places = db.Column(db.Text, nullable=True)

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    place_id = db.Column(db.Integer, db.ForeignKey('place.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)  # Correct usage


class State(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    state = db.Column(db.String(50), nullable=False)
    image = db.Column(db.String(100), nullable=False)
class PublicItinerary(db.Model):
    __tablename__ = 'public_itinerary'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    destination = db.Column(db.String(255), nullable=False)
    price = db.Column(db.Float, nullable=True)
    rating = db.Column(db.Float, nullable=True)
    booking_url = db.Column(db.String(255), nullable=False)
    image_url = db.Column(db.String(255), nullable=True)
    is_featured = db.Column(db.Boolean, default=False)
    day_wise_itineraries = db.relationship('DayWiseItinerary', backref='public_itinerary', lazy=True)

class DayWiseItinerary(db.Model):
    __tablename__ = 'day_wise_itinerary'
    id = db.Column(db.Integer, primary_key=True)
    itinerary_id = db.Column(db.Integer, db.ForeignKey('public_itinerary.id'), nullable=False)
    day_number = db.Column(db.Integer, nullable=False)
    date = db.Column(db.Date, nullable=False)
    location = db.Column(db.String(255), nullable=False)
    morning_activity = db.Column(db.String(255), nullable=True)
    afternoon_activity = db.Column(db.String(255), nullable=True)
    evening_activity = db.Column(db.String(255), nullable=True)
    accommodation = db.Column(db.String(255), nullable=True)
    transportation = db.Column(db.String(255), nullable=True)
    departure_time = db.Column(db.Time, nullable=True)
    arrival_time = db.Column(db.Time, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    photos = db.relationship('DayWisePhotos', backref='day_wise_itinerary', lazy=True)

class DayWisePhotos(db.Model):
    __tablename__ = 'day_wise_photos'
    id = db.Column(db.Integer, primary_key=True)
    day_itinerary_id = db.Column(db.Integer, db.ForeignKey('day_wise_itinerary.id'), nullable=False)
    image_url = db.Column(db.String(255), nullable=False)

class Booking(db.Model):
    __tablename__ = 'booking'
    
    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.Integer, db.ForeignKey('trips.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    contact_number = db.Column(db.String(20), nullable=False)
    num_travelers = db.Column(db.Integer, nullable=False)

    def __init__(self, trip_id, date, name, email, contact_number, num_travelers):
        self.trip_id = trip_id
        self.date = date
        self.name = name
        self.email = email
        self.contact_number = contact_number
        self.num_travelers = num_travelers
class ChatBot:
    def __init__(self, places):
        self.places = places
        self.questions = {
            0: {"text": "Hello! What's your mood today?", "options": ["Adventure", "Peace", "Nature", "Detox", "Culture"]},
            1: {
                "Adventure": {"text": "Do you prefer mountains or beaches?", "options": ["Mountains", "Beaches"]},
                "Peace": {"text": "Do you prefer staying in a quiet place or visiting cultural sites?", "options": ["Quiet Place", "Cultural Sites"]},
                "Nature": {"text": "Do you like forests or lakes?", "options": ["Forests", "Lakes"]},
                "Detox": {"text": "Are you interested in wellness retreats or eco-friendly stays?", "options": ["Wellness Retreats", "Eco-friendly Stays"]},
                "Culture": {"text": "Do you prefer historical places or modern attractions?", "options": ["Historical Places", "Modern Attractions"]}
            },
            2: {
                "Mountains": {"text": "Do you enjoy trekking or scenic views?", "options": ["Trekking", "Scenic Views"]},
                "Beaches": {"text": "Are you interested in water sports or relaxing by the shore?", "options": ["Water Sports", "Relaxing"]},
                "Quiet Place": {"text": "Do you prefer meditation or peaceful walks?", "options": ["Meditation", "Peaceful Walks"]},
                "Cultural Sites": {"text": "Are you more interested in museums or local markets?", "options": ["Museums", "Local Markets"]},
                "Forests": {"text": "Do you enjoy hiking or wildlife observation?", "options": ["Hiking", "Wildlife Observation"]},
                "Lakes": {"text": "Are you interested in boating or fishing?", "options": ["Boating", "Fishing"]},
                "Wellness Retreats": {"text": "Would you prefer spa treatments or yoga sessions?", "options": ["Spa Treatments", "Yoga Sessions"]},
                "Eco-friendly Stays": {"text": "Do you prefer organic food or sustainable living experiences?", "options": ["Organic Food", "Sustainable Living"]},
                "Historical Places": {"text": "Do you like visiting ancient monuments or historic towns?", "options": ["Ancient Monuments", "Historic Towns"]},
                "Modern Attractions": {"text": "Are you interested in modern architecture or nightlife?", "options": ["Modern Architecture", "Nightlife"]}
            },
            3: {"text": "Do you prefer unexplored or popular places?", "options": ["Unexplored", "Popular"]}
        }
        self.current_question = 0
        self.user_preferences = []

    def get_greeting(self):
        return {"message": self.questions[0]["text"], "options": self.questions[0]["options"]}

    def get_next_question(self):
        if self.current_question == 1:
            previous_response = self.user_preferences[0]
            question = self.questions[self.current_question].get(previous_response)
        elif self.current_question == 2:
            previous_response = self.user_preferences[1]
            question = self.questions[self.current_question].get(previous_response)
        elif self.current_question == 3:
            question = self.questions[self.current_question]
        else:
            return None

        self.current_question += 1
        return {"message": question["text"], "options": question["options"]}

    def get_response(self, user_input):
        self.user_preferences.append(user_input)
        next_question = self.get_next_question()
        if next_question:
            return next_question
        else:
            return self.match_places()

    def match_places(self):
        matched_places = []
        for place in self.places:
            if all(pref.lower() in [tag.lower() for tag in place['tags']] for pref in self.user_preferences):
                matched_places.append(place['name'])
        if matched_places:
            return {"message": "I found these places for you: " + ", ".join(matched_places)}
        else:
            return {"message": "I couldn't find any places that match your preferences."}

    def reset_chat(self):
        self.current_question = 1  # Keep the greeting as the first message
        self.user_preferences = []


chatbot = ChatBot(places)
# WTForms for Feedback and Contact
class FeedbackForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=2, max=50)])
    rating = IntegerField('Rating', validators=[DataRequired()])
    comment = TextAreaField('Comment', validators=[DataRequired(), Length(min=10)])
    submit = SubmitField('Submit Feedback')

class ContactForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(min=2, max=50)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    message = TextAreaField('Message', validators=[DataRequired(), Length(min=10)])
    submit = SubmitField('Send Message')

# Routes
@app.route('/')
def index():
    trending_places = Place.query.limit(6).all()  # Fetch 6 places to display
    return render_template('index.html', trending_places=trending_places)

@app.route('/explore')
def explore():
    states = State.query.all()
    return render_template('explore.html', states=states)

@app.route('/explore_state', methods=['GET', 'POST'])
def explore_state():
    if request.method == 'POST':
        state = request.form.get('state')
        places = Place.query.filter_by(state=state).all()
        return render_template('explore_state.html', places=places, state=state)
    return render_template('explore_state.html', places=[])

@app.route('/place/<int:place_id>', methods=['GET', 'POST'])
def place_detail(place_id):
    place = Place.query.get_or_404(place_id)
    feedback_form = FeedbackForm()
    feedbacks = Feedback.query.filter_by(place_id=place_id).all()

    if feedback_form.validate_on_submit():
        feedback = Feedback(
            place_id=place.id,
            username=feedback_form.username.data,
            rating=feedback_form.rating.data,
            comment=feedback_form.comment.data
        )
        db.session.add(feedback)
        db.session.commit()
        flash('Feedback submitted successfully!', 'success')
        return redirect(url_for('place_detail', place_id=place.id))

    return render_template('place_detail.html', place=place, feedback_form=feedback_form, feedbacks=feedbacks)

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    contact_form = ContactForm()
    if contact_form.validate_on_submit():
        # Handle contact form submission logic
        flash('Your message has been sent!', 'success')
        return redirect(url_for('contact'))
    return render_template('contact.html', contact_form=contact_form)

@app.route('/chatbot', methods=['POST'])
def chatbot_response():
    data = request.json
    user_input = data.get('message', '')
    if user_input.lower() == 'reset':
        chatbot.reset_chat()
        response = chatbot.get_greeting()
    else:
        response = chatbot.get_response(user_input)
    
    return jsonify(response)

@app.route('/explore_trips')
def explore_trips():
    trips = Trip.query.all()
    return render_template('explore_trips.html', trips=trips)

@app.route('/trip/<int:trip_id>')
def trip_itinerary(trip_id):
    trip = Trip.query.get_or_404(trip_id)
    itinerary = PublicItinerary.query.get_or_404(trip.itinerary_id)
    
    # Fetch day-wise itinerary details
    day_wise_itineraries = DayWiseItinerary.query.filter_by(itinerary_id=itinerary.id).order_by(DayWiseItinerary.day_number).all()

    # Fetch photos for each day
    for day in day_wise_itineraries:
        day.photos = DayWisePhotos.query.filter_by(day_itinerary_id=day.id).all()

    return render_template('trip_itinerary.html', trip=trip, itinerary=day_wise_itineraries)
@app.route('/booking/<int:trip_id>')
def booking_page(trip_id):
    session['trip_id'] = trip_id
    date = datetime.now().date()
    session['date'] = date.strftime('%Y-%m-%d')  # Store date in session as string

    return render_template('booking.html', trip_id=trip_id, date=date)

@app.route('/confirm_booking', methods=['POST'])
def confirm_booking():
    trip_id = session.get('trip_id')
    date_str = session.get('date')

    # Check if date_str is None
    if date_str is None:
        flash("Date is not set. Please try again.", "danger")
        return redirect(url_for('explore_trips'))

    # Convert date string to a date object
    booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    name = request.form.get('name')
    email = request.form.get('email')
    contact_number = request.form.get('contact_number')
    num_travelers = int(request.form.get('num_travelers'))

    # Create a new booking instance
    booking = Booking(
        trip_id=trip_id,
        date=booking_date,
        name=name,
        email=email,
        contact_number=contact_number,
        num_travelers=num_travelers
    )

    # Add the booking to the database and commit
    db.session.add(booking)
    db.session.commit()

    flash('Booking confirmed successfully!', 'success')
    return redirect(url_for('explore_trips'))

# Run the Application
if __name__ == '__main__':
    app.run(debug=True)