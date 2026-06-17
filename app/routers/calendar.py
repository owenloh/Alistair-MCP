"""Google Calendar connector endpoints.

HTTP mirror of Claude's Google Calendar connector — one POST endpoint per
connector tool, backed by the Google Calendar REST API v3. Tool descriptions are
copied verbatim from the connector so a voice-mode (HTTP-only) Claude can call
the same tools with the same descriptions.

Request models are defined inline here; each endpoint forwards parsed args to a
function in ``app.services.calendar``.
"""
from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter, Depends

from ..config import get_settings
from ..security import require_api_key
from ..services import calendar as calendar_service

router = APIRouter(
    prefix="/api/calendar",
    tags=["calendar"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReminderOverride(BaseModel):
    method: str
    minutes: int


class ListEventsRequest(BaseModel):
    calendarId: str | None = None
    startTime: str | None = None
    endTime: str | None = None
    timeZone: str | None = None
    fullText: str | None = None
    orderBy: str | None = None
    pageSize: int = 100
    pageToken: str | None = None
    singleEvents: bool = True


class ListCalendarsRequest(BaseModel):
    pageSize: int | None = None
    pageToken: str | None = None


class GetEventRequest(BaseModel):
    eventId: str
    calendarId: str | None = None


class CreateEventRequest(BaseModel):
    summary: str
    startTime: str
    endTime: str
    allDay: bool = False
    calendarId: str | None = None
    timeZone: str | None = None
    location: str | None = None
    description: str | None = None
    attendees: list[str] | None = None
    addGoogleMeetUrl: bool = False
    colorId: str | None = None
    visibility: str | None = None
    availability: str | None = None
    recurrence: list[str] | None = None
    reminders: list[ReminderOverride] | None = None
    notificationLevel: str | None = None


class UpdateEventRequest(BaseModel):
    eventId: str
    calendarId: str | None = None
    summary: str | None = None
    startTime: str | None = None
    endTime: str | None = None
    allDay: bool = False
    timeZone: str | None = None
    location: str | None = None
    description: str | None = None
    addedAttendees: list[str] | None = None
    removedAttendees: list[str] | None = None
    colorId: str | None = None
    visibility: str | None = None
    availability: str | None = None
    recurrence: list[str] | None = None
    reminders: list[ReminderOverride] | None = None
    addGoogleMeetUrl: bool = False
    notificationLevel: str | None = None


class DeleteEventRequest(BaseModel):
    eventId: str
    calendarId: str | None = None
    notificationLevel: str | None = None


class RespondToEventRequest(BaseModel):
    eventId: str
    responseStatus: str
    calendarId: str | None = None
    responseComment: str | None = None
    notificationLevel: str | None = None


class SuggestTimePreferences(BaseModel):
    startHour: str | None = None
    endHour: str | None = None
    excludeWeekends: bool | None = None
    pageSize: int = 5


class SuggestTimeRequest(BaseModel):
    attendeeEmails: list[str]
    startTime: str
    endTime: str
    durationMinutes: int = 30
    timeZone: str | None = None
    preferences: SuggestTimePreferences | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class TodayRequest(BaseModel):
    timeZone: str | None = None


@router.post(
    "/today",
    summary="Today's calendar events",
    description="Returns today's events (time, title, notes). Pass timeZone (IANA, "
    "e.g. America/New_York) to use a specific zone; if omitted, the service "
    "auto-detects your current Google Calendar timezone, falling back to the "
    "configured default. No body required.",
)
def today(body: TodayRequest | None = None) -> dict:
    return calendar_service.today_events(
        get_settings(), time_zone=body.timeZone if body else None
    )


@router.post(
    "/list-events",
    summary="List calendar events",
    description="Lists calendar events in a given calendar satisfying the given conditions. Retrieves ALL events matching the time constraints. Use for queries like: What's on my calendar tomorrow? What are my meetings next week? Do I have any conflicts this afternoon?",
)
def list_events(body: ListEventsRequest) -> dict:
    return calendar_service.list_events(
        get_settings(),
        calendar_id=body.calendarId,
        start_time=body.startTime,
        end_time=body.endTime,
        time_zone=body.timeZone,
        full_text=body.fullText,
        order_by=body.orderBy,
        page_size=body.pageSize,
        page_token=body.pageToken,
        single_events=body.singleEvents,
    )


@router.post(
    "/list-calendars",
    summary="List calendars",
    description="Returns the calendars on the user's calendar list. Use for queries like: What are all my calendars?",
)
def list_calendars(body: ListCalendarsRequest) -> dict:
    return calendar_service.list_calendars(
        get_settings(),
        page_size=body.pageSize,
        page_token=body.pageToken,
    )


@router.post(
    "/get-event",
    summary="Get a calendar event",
    description="Returns a single event from a given calendar. Use for queries like: Get details for the team meeting. Show me the event with id event123 on my calendar.",
)
def get_event(body: GetEventRequest) -> dict:
    return calendar_service.get_event(
        get_settings(),
        event_id=body.eventId,
        calendar_id=body.calendarId,
    )


@router.post(
    "/create-event",
    summary="Create a calendar event",
    description="Creates a calendar event. Use for queries like: Create an event on my calendar for tomorrow at 2pm called 'Meeting with Jane'. Schedule a meeting with john.doe@google.com next Monday from 10am to 11am.",
)
def create_event(body: CreateEventRequest) -> dict:
    return calendar_service.create_event(
        get_settings(),
        summary=body.summary,
        start_time=body.startTime,
        end_time=body.endTime,
        all_day=body.allDay,
        calendar_id=body.calendarId,
        time_zone=body.timeZone,
        location=body.location,
        description=body.description,
        attendees=body.attendees,
        add_google_meet_url=body.addGoogleMeetUrl,
        color_id=body.colorId,
        visibility=body.visibility,
        availability=body.availability,
        recurrence=body.recurrence,
        reminders=[r.model_dump() for r in body.reminders] if body.reminders is not None else None,
        notification_level=body.notificationLevel,
    )


@router.post(
    "/update-event",
    summary="Update a calendar event",
    description="Updates a calendar event. Use for queries like: Update the event 'Meeting with Jane' to be one hour later. Add john.doe@google.com to the meeting tomorrow.",
)
def update_event(body: UpdateEventRequest) -> dict:
    return calendar_service.update_event(
        get_settings(),
        event_id=body.eventId,
        calendar_id=body.calendarId,
        summary=body.summary,
        start_time=body.startTime,
        end_time=body.endTime,
        all_day=body.allDay,
        time_zone=body.timeZone,
        location=body.location,
        description=body.description,
        added_attendees=body.addedAttendees,
        removed_attendees=body.removedAttendees,
        color_id=body.colorId,
        visibility=body.visibility,
        availability=body.availability,
        recurrence=body.recurrence,
        reminders=[r.model_dump() for r in body.reminders] if body.reminders is not None else None,
        add_google_meet_url=body.addGoogleMeetUrl,
        notification_level=body.notificationLevel,
    )


@router.post(
    "/delete-event",
    summary="Delete a calendar event",
    description="Deletes a calendar event. To cancel or decline an event, use the respond_to_event tool instead. Use for queries like: Delete the event with id event123 on my calendar.",
)
def delete_event(body: DeleteEventRequest) -> dict:
    return calendar_service.delete_event(
        get_settings(),
        event_id=body.eventId,
        calendar_id=body.calendarId,
        notification_level=body.notificationLevel,
    )


@router.post(
    "/respond-to-event",
    summary="Respond to a calendar event",
    description="Responds to an event. Use for queries like: Accept the event with id event123 on my calendar. Decline the meeting with Jane. Tentatively accept the planning meeting.",
)
def respond_to_event(body: RespondToEventRequest) -> dict:
    return calendar_service.respond_to_event(
        get_settings(),
        event_id=body.eventId,
        response_status=body.responseStatus,
        calendar_id=body.calendarId,
        response_comment=body.responseComment,
        notification_level=body.notificationLevel,
    )


@router.post(
    "/suggest-time",
    summary="Suggest meeting times",
    description="Suggests time periods across one or more calendars. To access the primary calendar, add 'primary' in the attendee_emails field. Use for queries like: When are all of us free for a meeting? Find a 30 minute slot where we are both available. Check if jane.doe@google.com is free on Monday morning.",
)
def suggest_time(body: SuggestTimeRequest) -> dict:
    return calendar_service.suggest_time(
        get_settings(),
        attendee_emails=body.attendeeEmails,
        start_time=body.startTime,
        end_time=body.endTime,
        duration_minutes=body.durationMinutes,
        time_zone=body.timeZone,
        preferences=body.preferences.model_dump() if body.preferences is not None else None,
    )
