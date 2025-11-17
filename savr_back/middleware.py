from time import perf_counter
from django.db import connection, reset_queries
from django.conf import settings


class TimingMiddleware:
    """
    Middleware simple pour logguer le temps total et les requêtes SQL.
    Activé surtout en DEBUG.
    """
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        if settings.DEBUG:
            reset_queries()
            t0 = perf_counter()
            response = self.get_response(request)
            t1 = perf_counter()
            path = request.path
            if path.startswith('/api/meal-plans'):
                total_ms = (t1 - t0) * 1000
                num_queries = len(connection.queries)
                db_time_ms = sum(float(q.get('time', 0)) for q in connection.queries) * 1000
                print(f"[TimingMiddleware] {path} total_ms={total_ms:.1f} "
                      f"db_queries={num_queries} db_time_ms={db_time_ms:.1f}")
                # Expose timings to the client (Chrome DevTools 'Server-Timing' tab)
                try:
                    response.headers['Server-Timing'] = (
                        f"app;dur={total_ms:.1f}, db;dur={db_time_ms:.1f}, "
                        f"queries;desc=\"{num_queries} SQL\""
                    )
                except Exception:
                    pass
            return response
        return self.get_response(request)

