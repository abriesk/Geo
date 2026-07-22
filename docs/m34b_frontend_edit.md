# M3.4b frontend edit (surgical — apply to services/frontend/app.py)

The velocity PNG already renders (generic artifact loop). Only change: add the
date-window explainer. In the sidebar, REPLACE this block:

    use_dates = st.checkbox("Limit date range", value=False)
    dates = {"start": None, "end": None}
    if use_dates:
        c1, c2 = st.columns(2)
        dates["start"] = str(c1.date_input("From"))
        dates["end"] = str(c2.date_input("To"))

WITH:

    use_dates = st.checkbox(
        "Limit date range", value=False,
        help="Leave unchecked to use smart defaults — a suitable window is "
             "chosen automatically per analysis type.",
    )
    dates = {"start": None, "end": None}
    if use_dates:
        c1, c2 = st.columns(2)
        dates["start"] = str(c1.date_input("From"))
        dates["end"] = str(c2.date_input("To"))
        st.caption(
            "Window guidance: ground-motion (InSAR) needs at least 1 year, "
            "ideally 2+, or velocities are noisy. Flood works best in a narrow "
            "window around a specific event date. Vegetation compares dates "
            "about 1 year apart. Unsure? Uncheck for smart defaults."
        )
    else:
        st.caption(
            "Using smart defaults: ground-motion about 2 years, flood about "
            "3 months, vegetation about 1 year."
        )

Then: docker compose up --build -d frontend
