"""Description for the weather tool.

Single source of truth: both the HTTP router and the MCP @mcp.tool wrapper import
this, so every client sees the identical contract.
"""

WEATHER = (
    "Get the weather for a place and a TIME, instantly — one API call, no web search. "
    "Use this for ANY weather question: 'what's the weather', 'is it going to rain tonight', "
    "'weather in Tokyo tomorrow morning', 'do I need a coat this weekend', 'what's it like in "
    "a week', 'forecast for 3-7 Aug'. NEVER search the web for weather; call this instead. "
    "location: a place name ('Kuala Lumpur', 'Cambridge, UK'), a raw 'lat,lon' pair, or leave "
    "it out to use {user}'s own location (inferred from their live timezone, same as whereami). "
    "when: plain English — 'now', 'today', 'tonight', 'tomorrow', 'tomorrow morning', "
    "'this weekend', 'next Friday', 'in 3 days', 'in a week', 'next 5 days' — or exact dates "
    "and ranges ('2026-08-03', '3 Aug', '3-7 Aug', '2026-08-03 to 2026-08-07'). Everything is "
    "resolved in the DESTINATION's timezone, so 'tonight' means tonight where the weather is. "
    "Range: 92 days of history through 16 days ahead; anything further returns a clear error "
    "rather than a guess. units: 'metric' (default, °C/km/h/mm) or 'imperial' (°F/mph/inch). "
    "Returns current conditions, hour-by-hour detail for narrow windows, and per-day highs/lows, "
    "rain chance, wind, UV, sunrise/sunset — plus a one-line summary. Read-only, free, no key "
    "(Open-Meteo). Answer in Alistair's voice with what actually matters (coat? umbrella?); "
    "don't dump the whole table."
)
