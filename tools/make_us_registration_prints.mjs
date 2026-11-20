// Builds tests/fixtures/us_banner_concise.pdf, us_peoplesoft_list.pdf and us_workday_courses.pdf:
// replicas of the schedule prints three common US registration systems produce (Banner
// "Concise Student Schedule", PeopleSoft "My Class Schedule" list view, Workday "View My Courses").
// Every student, course and person is made up; only the layout is copied. Printed through Chromium,
// as a student prints the page from the browser. Needs Playwright from the Focusity web app:
//   node tools/make_us_registration_prints.mjs tests/fixtures
import { createRequire } from 'node:module';
const req = createRequire('C:/Users/user/Projects/focusity/package.json');
const { chromium } = req('playwright');
const OUT = process.argv[2];

// One student's fall term: course, title, sections with their meetings.
const COURSES = [
  { subj: 'MATH', num: '1210', title: 'Calculus I', cr: '4.000', sections: [
    { sec: '001', crn: '40213', comp: 'Lecture', days: 'MWF', pdays: 'MoWeFr', start: '09:00', end: '09:50', room: 'LCB 219', who: 'Carter, Ann' },
    { sec: '011', crn: '40227', comp: 'Discussion', days: 'R', pdays: 'Th', start: '10:45', end: '11:35', room: 'JWB 308', who: 'Reed, Sam' },
  ] },
  { subj: 'CS', num: '1410', title: 'Introduction to Object-Oriented Programming', cr: '4.000', sections: [
    { sec: '001', crn: '41150', comp: 'Lecture', days: 'TR', pdays: 'TuTh', start: '13:25', end: '14:45', room: 'WEB L104', who: 'Lee, Jordan' },
    { sec: '005', crn: '41162', comp: 'Laboratory', days: 'W', pdays: 'We', start: '15:00', end: '16:50', room: 'MEB 3225', who: 'Patel, Rina' },
  ] },
  { subj: 'WRTG', num: '2010', title: 'Intermediate Writing', cr: '3.000', sections: [
    { sec: '024', crn: '42318', comp: 'Seminar', days: 'TR', pdays: 'TuTh', start: '10:45', end: '12:05', room: 'LNCO 1105', who: 'Green, Maya' },
  ] },
  { subj: 'PSY', num: '1010', title: 'General Psychology', cr: '3.000', sections: [
    { sec: '002', crn: '43507', comp: 'Lecture', days: 'MW', pdays: 'MoWe', start: '12:55', end: '14:15', room: 'BEH S 110', who: 'Shah, Dev' },
  ] },
  { subj: 'ECON', num: '2010', title: 'Principles of Microeconomics', cr: '3.000', sections: [
    { sec: '090', crn: '44011', comp: 'Online', days: 'TBA', pdays: 'TBA', start: '', end: '', room: 'ONLINE', who: 'Hayes, Lou' },
  ] },
];
const ampm = (t, lower = false, pad = false) => {
  if (!t) return 'TBA';
  const [H, M] = t.split(':').map(Number);
  const h = ((H + 11) % 12) + 1;
  const s = `${pad ? String(h).padStart(2, '0') : h}:${String(M).padStart(2, '0')} ${H < 12 ? 'AM' : 'PM'}`;
  return lower ? s.toLowerCase() : s;
};
const css = `body{font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#000;margin:24px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:14px;margin:18px 0 6px}.muted{color:#444;margin:0 0 10px}
table{border-collapse:collapse;width:100%;margin-bottom:6px}th,td{border:1px solid #888;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#ddd}.box{border:1px solid #888;padding:8px;margin-bottom:14px;page-break-inside:avoid}`;

// A. Banner 8 "Concise Student Schedule": one row per meeting, Days + Time columns, Start/End Date columns.
function banner() {
  const rows = COURSES.flatMap((c) => c.sections.map((s) => `<tr><td>${s.crn}</td><td>${c.subj} ${c.num} ${s.sec}</td><td>${c.title}</td><td>Main</td><td>${c.cr}</td><td>Undergraduate</td><td>Aug 24, 2026</td><td>Dec 11, 2026</td><td>${s.days}</td><td>${s.start ? `${ampm(s.start, true)}-${ampm(s.end, true)}` : 'TBA'}</td><td>${s.room}</td><td>${s.who.split(', ').reverse().join(' ')} (P)</td></tr>`)).join('');
  return `<h1>Concise Student Schedule</h1><p class="muted">Fall Semester 2026 &nbsp;|&nbsp; Student: Alex Example &nbsp;|&nbsp; Total Credit Hours: 17.000</p>
<table><tr><th>CRN</th><th>Course</th><th>Title</th><th>Campus</th><th>Credits</th><th>Level</th><th>Start Date</th><th>End Date</th><th>Days</th><th>Time</th><th>Location</th><th>Instructor</th></tr>${rows}</table>`;
}

// B. PeopleSoft "My Class Schedule" list view: a box per course, course name in the box title,
// component rows with "Days & Times" packed ("MoWeFr 9:00AM - 9:50AM").
function peoplesoft() {
  return `<h1>My Class Schedule</h1><p class="muted">Fall 2026 | Undergraduate | State University</p>` + COURSES.map((c) => `<div class="box">
<h2>${c.subj} ${c.num} - ${c.title}</h2>
<table><tr><th>Status</th><th>Units</th><th>Grading</th><th>Deadlines</th></tr><tr><td>Enrolled</td><td>${parseFloat(c.cr).toFixed(2)}</td><td>Graded</td><td>Academic Calendar Deadlines</td></tr></table>
<table><tr><th>Class Nbr</th><th>Section</th><th>Component</th><th>Days &amp; Times</th><th>Room</th><th>Instructor</th><th>Start/End Date</th></tr>
${c.sections.map((s) => `<tr><td>${s.crn}</td><td>${s.sec}</td><td>${s.comp}</td><td>${s.start ? `${s.pdays} ${ampm(s.start).replace(' ', '')} - ${ampm(s.end).replace(' ', '')}` : 'TBA'}</td><td>${s.room}</td><td>${s.who.split(', ').reverse().join(' ')}</td><td>08/24/2026 - 12/11/2026</td></tr>`).join('')}
</table></div>`).join('');
}

// C. Workday "View My Courses": Course Listing "MATH 1210 - Calculus I", Meeting Patterns
// "MWF | 9:00 AM - 9:50 AM | LCB 219", Instructional Format.
const workdayRows = () => COURSES.flatMap((c) => c.sections.map((s) => [
  `${c.subj} ${c.num} - ${c.title}`, String(parseFloat(c.cr)), 'Graded',
  `${c.subj} ${c.num}-${s.sec} - ${c.title}`, s.comp === 'Laboratory' ? 'Lab' : s.comp, s.comp === 'Online' ? 'Online' : 'In-Person',
  s.start ? `${s.days.split('').join('/')} | ${ampm(s.start)} - ${ampm(s.end)} | ${s.room}` : '',
  'Registered', s.who.split(', ').reverse().join(' '), '08/24/2026', '12/11/2026',
]));
const WD_HEAD = ['Course Listing', 'Credits', 'Grading Basis', 'Section', 'Instructional Format', 'Delivery Mode', 'Meeting Patterns', 'Registration Status', 'Instructor', 'Start Date', 'End Date'];
function workday() {
  return `<h1>View My Courses</h1><p class="muted">Alex Example (1234567) &nbsp; 2026 Fall Semester (08/24/2026-12/11/2026)</p><h2>My Enrolled Courses &nbsp; 7 items</h2>
<table><tr>${WD_HEAD.map((h) => `<th>${h}</th>`).join('')}</tr>${workdayRows().map((r) => `<tr>${r.map((v) => `<td>${v}</td>`).join('')}</tr>`).join('')}</table>`;
}

const b = await chromium.launch();
const p = await b.newPage();
for (const [name, body] of [['us_banner_concise', banner()], ['us_peoplesoft_list', peoplesoft()], ['us_workday_courses', workday()]]) {
  await p.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>${css}</style></head><body>${body}</body></html>`);
  await p.pdf({ path: `${OUT}/${name}.pdf`, format: 'Letter', landscape: true, printBackground: true });
}
await b.close();
console.log('ok');
