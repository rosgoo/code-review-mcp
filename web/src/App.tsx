import { useEffect } from "react";
import { Link } from "./components/Link";
import { navigate, useRoute } from "./lib/router";
import { Inbox } from "./pages/Inbox";
import { ReviewRoute } from "./pages/ReviewRoute";

export function App() {
  const route = useRoute();
  const redirectTo = route.kind === "redirect" ? route.to : null;

  useEffect(() => {
    if (redirectTo !== null) navigate(redirectTo, { replace: true });
  }, [redirectTo]);

  switch (route.kind) {
    case "inbox":
      return <Inbox />;
    case "review":
      return <ReviewRoute key={route.id} reviewId={route.id} />;
    case "redirect":
      return null;
    case "not_found":
      return (
        <div className="empty-state">
          <p>Page not found.</p>
          <Link to="/">All reviews</Link>
        </div>
      );
  }
}
