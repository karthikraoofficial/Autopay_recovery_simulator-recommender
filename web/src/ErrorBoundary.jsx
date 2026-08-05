import React from "react";

// One section failing must never take the page with it.
//
// A runtime error anywhere in the tree unmounts the *whole* React application by default,
// so a panel that could not list mapping profiles blanked the simulator, the headline and
// the exports along with itself. The value of the page is not uniform: losing the upload
// section is an inconvenience, losing the result the user just waited minutes for is not.
//
// A class component because `componentDidCatch` has no hook equivalent.
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Kept: the fallback says a section failed, and the console says where. Swallowing it
    // entirely would trade a blank page for a silent one.
    console.error(`[${this.props.section ?? "section"}] failed and was contained:`, error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section>
        <h2>{this.props.section ?? "This section"} is unavailable</h2>
        <p className="error">
          It failed with: {String(this.state.error.message ?? this.state.error)}
        </p>
        <p className="hint">
          The rest of this page is unaffected — nothing above or below this block depends on it. The
          full error is in the browser console.
        </p>
      </section>
    );
  }
}
