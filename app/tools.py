"""Tools the LLM can call. Same tools for chatbot and voicebot -> same backend, same behaviour."""
from . import calendar_service as cal, db, notifier

CalendarError = cal.CalendarError


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


_S = lambda d: {"type": "string", "description": d}

TOOLS = [
    _fn("check_availability",
        "Get real-time free appointment times for a service on a date. ALWAYS call this before offering or confirming any time.",
        {"service_id": _S("Service id from the services list"), "date": _S("YYYY-MM-DD"),
         "dentist": _S("Optional dentist name if the patient asked for one")},
        ["service_id", "date"]),
    _fn("book_appointment",
        "Book an appointment AFTER the patient has confirmed service, date, time, name and phone. Sends a confirmation automatically.",
        {"service_id": _S("Service id"), "date": _S("YYYY-MM-DD"), "time": _S("HH:MM 24h, must be one of the available times"),
         "patient_name": _S("Full name"), "phone": _S("Phone number (omit if the channel already provides it and the patient confirmed it)"),
         "email": _S("Optional email for the confirmation"), "dentist": _S("Optional preferred dentist"),
         "notes": _S("Optional note, e.g. symptoms")},
        ["service_id", "date", "time", "patient_name"]),
    _fn("find_appointments", "List the upcoming appointments for a phone number.",
        {"phone": _S("Phone number (omit to use the caller's number)")}, []),
    _fn("reschedule_appointment", "Move an existing booking to a new date/time. Check availability first.",
        {"reference": _S("Booking reference, e.g. BS-7K3Q9"), "new_date": _S("YYYY-MM-DD"), "new_time": _S("HH:MM"),
         "phone": _S("Phone number on the booking (omit to use the caller's number)")},
        ["reference", "new_date", "new_time"]),
    _fn("cancel_appointment", "Cancel an existing booking after the patient clearly confirmed.",
        {"reference": _S("Booking reference"), "phone": _S("Phone number on the booking (omit to use the caller's number)")},
        ["reference"]),
    _fn("save_customer_details", "Save contact details of a person who wants a callback or more info but is not booking now.",
        {"name": _S("Name"), "phone": _S("Phone"), "email": _S("Email"), "notes": _S("What they need")}, ["name"]),
    _fn("transfer_to_human",
        "Hand the conversation to a staff member. Use for: clinical/medical advice, insurance or billing disputes, complaints, "
        "angry customers, requests for a human, or anything you cannot resolve with the tools.",
        {"reason": _S("Short reason"), "summary": _S("One-paragraph summary so staff do not have to ask again")},
        ["reason", "summary"]),
]


def _summ(a):
    return {"reference": a["ref"], "service": a["service_name"], "dentist": a["dentist"], "when": cal.human(a),
            "date": a["start"][:10], "time": a["start"][11:16], "status": a["status"], "patient": a["name"]}


def run_tool(name, args, ctx):
    """Execute a tool. Never raises: errors are returned to the model so it can recover gracefully."""
    try:
        phone = args.get("phone") or ctx.get("phone")
        if name == "check_availability":
            return {"ok": True, **cal.get_availability(args["date"], args["service_id"], args.get("dentist"))}
        if name == "book_appointment":
            appt = cal.book(args["service_id"], args["date"], args["time"], args.get("patient_name"), phone,
                            args.get("email"), args.get("dentist"), args.get("notes"), ctx.get("channel", "web"))
            if appt.pop("_existing", False):
                return {"ok": True, **_summ(appt), "already_booked": True,
                        "note": "This exact appointment was already booked earlier. Confirm it to the person with the reference. Do not book again."}
            sent = notifier.send_confirmation(appt, "booked")
            return {"ok": True, **_summ(appt), "confirmation_sent_via": sent}
        if name == "find_appointments":
            if not phone:
                return {"ok": False, "error": "A phone number is needed to look up bookings."}
            return {"ok": True, "appointments": [_summ(a) for a in cal.upcoming(phone)]}
        if name == "reschedule_appointment":
            appt = cal.reschedule(args["reference"], phone, args["new_date"], args["new_time"])
            sent = notifier.send_confirmation(appt, "rescheduled")
            return {"ok": True, **_summ(appt), "confirmation_sent_via": sent}
        if name == "cancel_appointment":
            appt = cal.cancel(args["reference"], phone)
            sent = notifier.send_confirmation(appt, "cancelled")
            return {"ok": True, **_summ(appt), "confirmation_sent_via": sent}
        if name == "save_customer_details":
            p = cal.norm_phone(phone)
            with db.get_db() as conn:
                db.upsert_customer(conn, args.get("name", ""), p, args.get("email"))
            db.create_handoff(ctx["session_id"], ctx["channel"], p, "callback request",
                              f"{args.get('name')}: {args.get('notes', '')}")
            return {"ok": True, "saved": True}
        if name == "transfer_to_human":
            hid = db.create_handoff(ctx["session_id"], ctx["channel"], phone, args.get("reason", ""), args.get("summary", ""))
            notifier.notify_staff(hid, ctx["channel"], phone, args.get("reason", ""), args.get("summary", ""))
            ctx["handoff"] = True
            return {"ok": True, "note": "Staff have been notified with your summary. Tell the person a team member will take over shortly."}
        return {"ok": False, "error": f"Unknown tool {name}"}
    except CalendarError as e:
        return {"ok": False, "error": str(e)}
    except KeyError as e:
        return {"ok": False, "error": f"Missing required argument {e}"}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"Internal error: {type(e).__name__}"}