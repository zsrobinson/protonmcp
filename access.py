"""Cloudflare Access verification, mirroring services/print/server.py."""
import logging
import os
import re


class AccessJWT:
    def __init__(self, team_domain, audience):
        import jwt
        self.jwt = jwt
        self.issuer = f'https://{team_domain}'
        self.audience = audience
        # Same cache lifetime, key policy and network timeout as the print service.
        self.jwks = jwt.PyJWKClient(f'{self.issuer}/cdn-cgi/access/certs',
                                   cache_jwk_set=True, cache_keys=False,
                                   lifespan=300, timeout=5)

    def validate(self, token):
        jwt = self.jwt
        if len(token) > 16384 or jwt.get_unverified_header(token).get('alg') != 'RS256':
            raise jwt.InvalidTokenError('Expected RS256 assertion')
        key = self.jwks.get_signing_key_from_jwt(token)
        jwt.decode(token, key.key, algorithms=['RS256'], audience=self.audience,
                   issuer=self.issuer, options={'require': ['aud', 'iss', 'exp']})

    def authorized(self, tokens):
        try:
            if len(tokens) != 1 or not tokens[0]:
                return False
            self.validate(tokens[0])
            return True
        except self.jwt.PyJWTError:
            # Never return/log assertions or validation/JWKS failure details.
            return False


def configured_access():
    team = os.environ.get('ACCESS_TEAM_DOMAIN', '').strip()
    audience = os.environ.get('ACCESS_AUD', '').strip()
    if bool(team) != bool(audience):
        raise ValueError('Set both ACCESS_TEAM_DOMAIN and ACCESS_AUD, or neither')
    if team:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com', team):
            raise ValueError('ACCESS_TEAM_DOMAIN must be <team>.cloudflareaccess.com without a scheme or path')
        return AccessJWT(team, audience)
    logging.getLogger(__name__).warning(
        'Access JWT validation is DISABLED: ACCESS_TEAM_DOMAIN and ACCESS_AUD are unset; '
        'localhost-only mode. Configure both before exposing the service.')
    return None
