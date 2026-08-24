import Card from "../components/Card";
import PolicyNavigator from "../components/PolicyNavigator";

// w-9-2-planner P3 — minimal UI: a single question, a truthful provider/
// corpus label, a cited response, and refusal/error display. No booking,
// no editing, nothing else on this page.
export default function PolicyPage() {
  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Policy navigator</h1>
        <p>Ask how an approved synthetic Riverbend workflow is supposed to work.</p>
      </div>
      <Card>
        <PolicyNavigator />
      </Card>
    </div>
  );
}
