/* "Event date" of the photo-report forms (photo_reports.html, the admin's
   crm_photo_report_create.html): picking a task / event pre-fills the date
   with its own one (task: the completion day, else event: its day — options
   carry data-date, forms._DatedSelect) when it is inside the allowed window
   (the input's min / max), else today (max) — the same rule as the server's
   _EventDateMixin. Once the person edits the date, it is left alone. */
(function () {
    var input = document.querySelector('[data-pr-event-date]');
    if (!input) return;
    var task = document.querySelector('[data-pr-task]');
    var event = document.querySelector('[data-pr-event]');
    var touched = false;
    input.addEventListener('input', function () { touched = true; });

    function dateOf(select) {
        var option = select && select.options[select.selectedIndex];
        return option ? option.getAttribute('data-date') : null;
    }
    function apply() {
        if (touched) return;
        var day = dateOf(task) || dateOf(event);
        var inside = day && (!input.min || day >= input.min) && (!input.max || day <= input.max);
        input.value = inside ? day : input.max;
    }
    [task, event].forEach(function (select) {
        if (select) select.addEventListener('change', apply);
    });
})();
