import json
import secrets
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

APPLIANCE_KINDS = {
    "oven": "Oven",
    "smoker": "Smoker",
    "grill": "Grill",
    "air_fryer": "Air fryer",
    "burner": "Burner",
    "slow_cooker": "Slow cooker",
    "pressure_cooker": "Pressure cooker",
    "microwave": "Microwave",
    "other": "Other",
}
HEATED_KINDS = {"oven", "smoker", "grill", "air_fryer"}

COURSES = ["appetizer", "main", "side", "bread", "dessert", "drink", "other"]
RSVPS = ["invited", "yes", "maybe", "no"]

# The "generalware" list from the original spreadsheet; per_guest items scale with headcount.
DEFAULT_GENERALWARE = [
    ("table cloths", 3, None),
    ("napkins", None, 2),
    ("table runners", 3, None),
    ("china", None, 2),
    ("silverware", None, 2),
    ("glasses", None, 2),
    ("salt & pepper", 3, None),
    ("butter", 3, None),
    ("tables", 3, None),
]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_token():
    return secrets.token_urlsafe(18)


class JSONText(db.TypeDecorator):
    impl = db.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class KitchenSettings(db.Model):
    household_id = db.Column(db.String(64), primary_key=True)
    temp_tolerance = db.Column(db.Integer, nullable=False, default=25)
    minutes_per_10f = db.Column(db.Float, nullable=False, default=1.0)
    preheat_minutes = db.Column(db.Integer, nullable=False, default=15)
    default_hold = db.Column(db.Integer, nullable=False, default=20)
    cooks = db.Column(db.Integer, nullable=False, default=1)
    temp_unit = db.Column(db.String(1), nullable=False, default="F", server_default="F")  # F or C, for every temperature shown

    @property
    def deg(self):
        return "°" + self.temp_unit


class Appliance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.String(64), index=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=1)
    sort = db.Column(db.Integer, nullable=False, default=0)

    @property
    def heated(self):
        return self.kind in HEATED_KINDS


class DietaryFlag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.String(64), index=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    safe_tags = db.Column(JSONText, nullable=False, default=list)
    caution_tags = db.Column(JSONText, nullable=False, default=list)
    unsafe_tags = db.Column(JSONText, nullable=False, default=list)
    sort = db.Column(db.Integer, nullable=False, default=0)


guest_flags = db.Table(
    "guest_flags",
    db.Column("guest_id", db.Integer, db.ForeignKey("guest.id", ondelete="CASCADE"), primary_key=True),
    db.Column("flag_id", db.Integer, db.ForeignKey("dietary_flag.id", ondelete="CASCADE"), primary_key=True),
)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.String(64), index=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    date = db.Column(db.Date, nullable=False)
    serve_time = db.Column(db.Time, nullable=False)
    location = db.Column(db.String(200), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    guest_token = db.Column(db.String(40), unique=True, nullable=False, default=new_token)
    guest_link_enabled = db.Column(db.Boolean, nullable=False, default=True)
    shopping_list_id = db.Column(db.String(64))
    group_slug = db.Column(db.String(120))  # Mealie group, for recipe links on the guest page
    created_by = db.Column(db.String(120), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    parties = db.relationship("Party", back_populates="event", cascade="all, delete-orphan", order_by="Party.sort, Party.id")
    guests = db.relationship("Guest", back_populates="event", cascade="all, delete-orphan", order_by="Guest.id")
    dishes = db.relationship("Dish", back_populates="event", cascade="all, delete-orphan", order_by="Dish.sort, Dish.id")
    prep_tasks = db.relationship(
        "PrepTask", back_populates="event", cascade="all, delete-orphan", order_by="PrepTask.days_before.desc(), PrepTask.id"
    )
    checklist = db.relationship(
        "ChecklistItem", back_populates="event", cascade="all, delete-orphan", order_by="ChecklistItem.sort, ChecklistItem.id"
    )
    tool_checks = db.relationship("ToolCheck", cascade="all, delete-orphan")

    @property
    def attending(self):
        return [g for g in self.guests if g.rsvp != "no"]

    @property
    def headcount(self):
        """Adults count 1, children 0.5; declined guests don't count."""
        return sum(1 if g.age == "adult" else 0.5 for g in self.attending)

    @property
    def people(self):
        return len(self.attending)


class Party(db.Model):
    """A group that RSVPs together (a household or couple). Anyone in it can answer for all of it."""

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), index=True, nullable=False)
    sort = db.Column(db.Integer, nullable=False, default=0)

    event = db.relationship("Event", back_populates="parties")
    members = db.relationship("Guest", back_populates="party", order_by="Guest.id")

    @property
    def label(self):
        names = [g.name for g in self.members]
        if len(names) <= 2:
            return " & ".join(names)
        return ", ".join(names[:-1]) + " & " + names[-1]


class Guest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), index=True, nullable=False)
    party_id = db.Column(db.Integer, db.ForeignKey("party.id", ondelete="CASCADE"), index=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    age = db.Column(db.String(10), nullable=False, default="adult")
    rsvp = db.Column(db.String(10), nullable=False, default="invited")
    note = db.Column(db.String(200), nullable=False, default="")

    event = db.relationship("Event", back_populates="guests")
    party = db.relationship("Party", back_populates="members")
    flags = db.relationship("DietaryFlag", secondary=guest_flags, order_by="DietaryFlag.sort, DietaryFlag.id")


class Dish(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), index=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    course = db.Column(db.String(20), nullable=False, default="side")
    bringer = db.Column(db.String(10), nullable=False, default="host")  # host | guest | open
    bringer_guest_id = db.Column(db.Integer, db.ForeignKey("guest.id", ondelete="SET NULL"))
    mealie_id = db.Column(db.String(64))
    mealie_slug = db.Column(db.String(200))
    recipe_servings = db.Column(db.Float)
    scale = db.Column(db.Float)  # None = automatic from headcount
    mealie_tags = db.Column(JSONText, nullable=False, default=list)
    mealie_tools = db.Column(JSONText, nullable=False, default=list)
    diet = db.Column(JSONText, nullable=False, default=dict)  # flag id (str) -> status, for non-Mealie dishes
    serveware = db.Column(db.String(200), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    ready_at = db.Column(db.Integer, nullable=False, default=0)
    max_hold = db.Column(db.Integer)
    arrive_time = db.Column(db.Time)
    guest_added = db.Column(db.Boolean, nullable=False, default=False)
    synced_at = db.Column(db.DateTime)
    sort = db.Column(db.Integer, nullable=False, default=0)

    event = db.relationship("Event", back_populates="dishes")
    bringer_guest = db.relationship("Guest")
    steps = db.relationship("Step", back_populates="dish", cascade="all, delete-orphan", order_by="Step.seq, Step.id")

    @property
    def from_mealie(self):
        return bool(self.mealie_id)

    @property
    def who(self):
        if self.bringer == "host":
            return "Host"
        if self.bringer == "open":
            return "Needs someone"
        return self.bringer_guest.name if self.bringer_guest else "A guest"


class Step(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dish_id = db.Column(db.Integer, db.ForeignKey("dish.id", ondelete="CASCADE"), index=True, nullable=False)
    seq = db.Column(db.Integer, nullable=False, default=0)
    source_id = db.Column(db.String(64))  # Mealie step number this step's minutes come from
    source_text = db.Column(db.String(60))  # start of that step's text, to notice reordering
    label = db.Column(db.String(80), nullable=False)
    minutes = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(20))  # None = counter / resting
    appliance_id = db.Column(db.Integer, db.ForeignKey("appliance.id", ondelete="SET NULL"))
    temp = db.Column(db.Integer)
    hands_on = db.Column(db.Boolean, nullable=False, default=False)

    dish = db.relationship("Dish", back_populates="steps")
    appliance = db.relationship("Appliance")

    def as_dict(self):
        return {
            "source_id": self.source_id,
            "source_text": self.source_text,
            "label": self.label,
            "minutes": self.minutes,
            "kind": self.kind,
            "appliance_id": self.appliance_id,
            "temp": self.temp,
            "hands_on": self.hands_on,
        }


class RecipeTiming(db.Model):
    """What the host set for a Mealie recipe that Mealie can't hold yet: each step's appliance,
    temperature and hands-on flag (keyed by Mealie instruction id), plus ready/hold. Minutes are
    never stored here; they always come from Mealie."""

    household_id = db.Column(db.String(64), primary_key=True)
    mealie_id = db.Column(db.String(64), primary_key=True)
    steps = db.Column(JSONText, nullable=False, default=list)
    ready_at = db.Column(db.Integer, nullable=False, default=0)
    max_hold = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class PrepTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), index=True, nullable=False)
    dish_id = db.Column(db.Integer, db.ForeignKey("dish.id", ondelete="SET NULL"))
    label = db.Column(db.String(200), nullable=False)
    days_before = db.Column(db.Integer, nullable=False, default=1)
    done = db.Column(db.Boolean, nullable=False, default=False)

    event = db.relationship("Event", back_populates="prep_tasks")
    dish = db.relationship("Dish")


class ChecklistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), index=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    qty = db.Column(db.Integer)  # fixed count
    per_guest = db.Column(db.Float)  # or a count per attending person
    done = db.Column(db.Boolean, nullable=False, default=False)
    sort = db.Column(db.Integer, nullable=False, default=0)

    event = db.relationship("Event", back_populates="checklist")

    def count(self, people):
        if self.per_guest is not None:
            return int(round(self.per_guest * people))
        return self.qty


class ToolCheck(db.Model):
    """A cookware item (from the menu's Mealie tools) ticked off for an event."""

    event_id = db.Column(db.Integer, db.ForeignKey("event.id", ondelete="CASCADE"), primary_key=True)
    name = db.Column(db.String(120), primary_key=True)
