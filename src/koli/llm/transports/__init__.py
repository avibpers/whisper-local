# llm/transports
# One module per wire format. Each exposes complete(...) -> (text, tokens_in,
# tokens_out) and raises http.HttpError / http.NetworkError / ProviderError;
# the provider entries in llm/providers.py choose the URL and headers.
