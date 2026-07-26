import { useEffect, useState } from "react";
import { api } from "../api";
import { identityHue } from "../avatar";

// The byline (agents-expansion plan A0, invariant 2): a member's face and
// name, rendered the same everywhere an attribution appears — next to a
// message, a contribution (from A1 on), a story's sources (A2). The photo
// is the account's published choice; without one, the generated identity
// color stands in, so a byline never renders blank.

export function Byline({
  username,
  size = 28,
}: {
  username: string;
  size?: number;
}) {
  const [name, setName] = useState(username);
  const [photo, setPhoto] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let url: string | null = null;
    setName(username);
    setPhoto(null);
    void api
      .profile(username)
      .then(async (p) => {
        if (!alive) return;
        if (p.display_name) setName(p.display_name);
        if (p.has_photo) {
          url = await api.profilePhotoUrl(username);
          if (alive && url) setPhoto(url);
        }
      })
      .catch(() => {}); // no profile door here: username + color stand
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [username]);

  return (
    <span className="byline" title={username}>
      {photo ? (
        <img
          className="byline-photo"
          src={photo}
          alt=""
          style={{ width: size, height: size }}
        />
      ) : (
        <span
          className="byline-photo byline-fallback"
          style={{
            width: size,
            height: size,
            background: `hsl(${identityHue(username)} 45% 34%)`,
          }}
        >
          {name.slice(0, 1).toUpperCase()}
        </span>
      )}
      <span className="byline-name">{name}</span>
    </span>
  );
}
