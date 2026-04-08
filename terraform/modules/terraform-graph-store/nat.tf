# ── NAT Gateway (optional) ────────────────────────────────────────────────────
# VPC-attached CodeBuild cannot reach the internet through an IGW even when
# placed in a public subnet. A NAT gateway is required.
# When create_nat_gateway = true this creates:
#   - an EIP + NAT GW in the first public subnet (var.subnet_ids[0])
#   - a private subnet in the same AZ for CodeBuild
#   - a route table routing 0.0.0.0/0 through the NAT GW

data "aws_subnet" "nat_public" {
  id = var.subnet_ids[0]
}

resource "aws_eip" "nat" {
  count  = var.create_nat_gateway ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${var.cluster_identifier}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  count         = var.create_nat_gateway ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = var.subnet_ids[0]
  tags          = { Name = "${var.cluster_identifier}-nat-gw" }
  depends_on    = [aws_eip.nat]
}

resource "aws_subnet" "codebuild_private" {
  count             = var.create_nat_gateway ? 1 : 0
  vpc_id            = var.vpc_id
  cidr_block        = var.codebuild_private_subnet_cidr
  availability_zone = data.aws_subnet.nat_public.availability_zone
  tags              = { Name = "${var.cluster_identifier}-codebuild-private" }
}

resource "aws_route_table" "codebuild_private" {
  count  = var.create_nat_gateway ? 1 : 0
  vpc_id = var.vpc_id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[0].id
  }

  tags = { Name = "${var.cluster_identifier}-codebuild-rt" }
}

resource "aws_route_table_association" "codebuild_private" {
  count          = var.create_nat_gateway ? 1 : 0
  subnet_id      = aws_subnet.codebuild_private[0].id
  route_table_id = aws_route_table.codebuild_private[0].id
}
