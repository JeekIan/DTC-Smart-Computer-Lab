"""Single rule for every email in CompuLab: Gmail only."""

# Common mistypes of "gmail.com" — used only to give a friendlier hint.
GMAIL_TYPO_DOMAINS = {
    'mail.com', 'gmial.com', 'gmai.com', 'gmal.com', 'gamil.com', 'gnail.com',
    'gmil.com', 'gmaill.com', 'gmail.co', 'gmail.con', 'gmail.cm', 'gmail.om',
    'gmail.comm', 'gmail.vom', 'gmail.xom',
}


def email_domain_error(email):
    """Returns an error message if the address isn't a @gmail.com address,
    else None. Blank values are handled by each form's own required-check."""
    if not email:
        return None
    email = email.strip()
    if '@' not in email:
        return None  # EmailField's own format validation reports this
    domain = email.rsplit('@', 1)[1].strip().lower()
    if domain == 'gmail.com':
        return None
    if domain in GMAIL_TYPO_DOMAINS:
        return f'"@{domain}" looks like a typo. Did you mean "@gmail.com"? Please double-check the address.'
    return 'Only Gmail addresses (@gmail.com) are accepted.'
