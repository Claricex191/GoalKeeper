// Only a user id is chosen here; role and team are resolved by the backend
// (X-Demo-User header). Switching user starts new sessions in both panels.
export default function UserSwitcher({ users, value, onChange }) {
  const me = users.find((u) => u.user === value);
  return (
    <label className="userswitcher">
      <span className="userswitcher-label">Signed in as</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {users.map((u) => (
          <option key={u.user} value={u.user}>
            {u.display_name}
          </option>
        ))}
      </select>
      {me && (
        <span className="userswitcher-meta">
          role <b>{me.role}</b> · team <b>{me.team}</b>
        </span>
      )}
    </label>
  );
}
