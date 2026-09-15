# Load balancer for the API replicas.
#
# The health check is the point: `/health/ready` reports whether *this* replica
# can reach the database, so a replica that has lost it stops receiving traffic
# while staying alive — which keeps its logs and metrics available to explain
# what happened. A liveness check here would have taken it out only after
# something restarted it.
:80 {
	encode gzip zstd

	reverse_proxy api-1:4747 api-2:4747 {
		lb_policy least_conn

		health_uri /health/ready
		health_interval 10s
		health_timeout 5s
		health_status 2xx

		# MCP is Streamable HTTP / SSE: responses must not be buffered.
		flush_interval -1
	}
}
