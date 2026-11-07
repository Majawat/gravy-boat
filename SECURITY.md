# Security

Please don't open a public issue for a security problem. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/Majawat/gravy-boat/security/advisories/new)
on this repository. You'll get a reply as soon as possible.

Gravy Boat is meant to run behind HTTPS, with Mealie handling sign-in. Things worth knowing:
- The Mealie access token of a signed-in host is kept in a signed, HttpOnly session cookie.
- A guest link is the only key to an event's guest page; anyone holding it can pick any name on it.
  Hosts can turn a link off or replace it.
